"""
Independent watchdog for the PI Mismatch transfer service.

Run every 5 minutes from Windows Task Scheduler (NOT inside the service,
so it still works when the service is dead or hung). It looks at the
folders and the service's heartbeat directly, and raises alerts through
channels that do not need SMTP:

  * Windows Application Event Log (eventcreate.exe, built into Windows)
    -> picked up by SCOM / Zabbix / Splunk / any Windows monitoring agent
  * Microsoft Teams (Workflows webhook) or any generic JSON webhook, if
    outbound HTTPS is allowed
  * health.json / HEALTH.txt in publish_dir (optional, off by default; use a
    NEW folder - never one of the existing tool folders)

Alerts are de-duplicated: sent when a problem starts, repeated every
alert_repeat_seconds while it lasts, and a RESOLVED message when it clears.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple

from common import (
    Config,
    hostname,
    load_config,
    parse_iso,
    read_json,
    setup_logging,
    utc_now_iso,
    write_json_atomic,
)

log = logging.getLogger("healthcheck")

Problem = Tuple[str, str, str]  # (key, severity, message)


# ==========================================
# Checks
# ==========================================

def oldest_age(folder: Path, pattern: str = "*.json") -> Tuple[int, float]:
    now = time.time()
    ages = []
    if folder.exists():
        for p in folder.glob(pattern):
            try:
                st = p.stat()
                # ctime = creation time on Windows (a copied-in file keeps its old mtime)
                ages.append(now - max(st.st_mtime, st.st_ctime))
            except FileNotFoundError:
                pass
    return len(ages), max(ages, default=0.0)


def run_checks(cfg: Config) -> Tuple[List[Problem], Dict]:
    problems: List[Problem] = []
    status = read_json(cfg.status_file, default={}) or {}

    heartbeat = parse_iso(status.get("heartbeat_utc"))
    heartbeat_age = (time.time() - heartbeat) if heartbeat else None
    if heartbeat_age is None or heartbeat_age > cfg.heartbeat_stale_seconds:
        age_txt = "never" if heartbeat_age is None else f"{heartbeat_age / 60:.0f} min ago"
        problems.append(("service_down", "ERROR",
                         f"Transfer service is not running or is hung (last heartbeat: {age_txt})."))

    if status.get("connection_ok") is False:
        problems.append(("sftp_unreachable", "ERROR",
                         f"Cannot connect to SFTP server {cfg.sftp_host}: {status.get('last_error')}"))

    # Checked straight from the folder, so it also works when the service is
    # dead, or the producer keeps files locked.
    n, age = oldest_age(cfg.inbox_dir)
    if n and age > cfg.backlog_alert_seconds:
        problems.append(("queue_backlog", "ERROR",
                         f"{n} file(s) in {cfg.inbox_dir} not sent yet; oldest arrived "
                         f"{age / 60:.0f} min ago. Last error: {status.get('last_error')}"))

    invalid = status.get("invalid_files") or []
    if invalid:
        problems.append(("invalid_json", "ERROR",
                         f"{len(invalid)} file(s) in {cfg.inbox_dir} are not valid JSON and are "
                         f"NOT being sent: {', '.join(invalid[:10])}. Fix or replace them; "
                         "they are re-checked automatically."))

    n, age = oldest_age(cfg.uploaded_dir)
    if n and age > cfg.archive_backlog_alert_seconds:
        problems.append(("archive_backlog", "WARNING",
                         f"{n} sent file(s) could not be archived to {cfg.archive_dir} "
                         f"for {age / 60:.0f} min: {status.get('archive_error')}"))

    return problems, status


# ==========================================
# Alert channels
# ==========================================

def alert_event_log(cfg: Config, severity: str, message: str) -> None:
    level = {"ERROR": "ERROR", "WARNING": "WARNING"}.get(severity, "INFORMATION")
    event_id = {"ERROR": "900", "WARNING": "800"}.get(severity, "100")
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            ["eventcreate", "/T", level, "/ID", event_id, "/L", "APPLICATION",
             "/SO", cfg.event_log_source, "/D", message[:30000]],
            check=True, capture_output=True, timeout=30,
        )
    except Exception as exc:
        log.warning("Could not write to Event Log: %s", exc)


def alert_webhook(cfg: Config, severity: str, message: str) -> None:
    if not cfg.webhook_url:
        return
    title = f"PI Mismatch Transfer - {severity} on {hostname()}"
    if cfg.webhook_kind == "teams":
        # Teams "Workflows" (Power Automate) webhook expects an Adaptive Card.
        payload = {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True,
                         "color": "Attention" if severity == "ERROR" else "Default"},
                        {"type": "TextBlock", "text": message, "wrap": True},
                        {"type": "TextBlock", "text": utc_now_iso(), "isSubtle": True},
                    ],
                },
            }],
        }
    else:  # generic: Slack / Mattermost / Google Chat / custom endpoints accept {"text": ...}
        payload = {"text": f"*{title}*\n{message}"}

    req = urllib.request.Request(
        cfg.webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except Exception as exc:
        log.warning("Webhook alert failed: %s", exc)


def send_alert(cfg: Config, severity: str, message: str) -> None:
    level = {"ERROR": logging.ERROR, "WARNING": logging.WARNING}.get(severity, logging.INFO)
    log.log(level, "ALERT: %s", message)
    alert_event_log(cfg, severity, message)
    alert_webhook(cfg, severity, message)


# ==========================================
# Self-healing
# ==========================================

def restart_service(cfg: Config) -> None:
    if sys.platform != "win32":
        return
    log.warning("Restarting Windows service %s ...", cfg.service_name)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"Restart-Service -Name '{cfg.service_name}' -Force"],
            check=True, capture_output=True, timeout=120,
        )
    except Exception as exc:
        log.error("Service restart failed: %s", exc)


# ==========================================
# Main
# ==========================================

def main() -> int:
    cfg = load_config()
    setup_logging(cfg, "healthcheck")

    state_file = cfg.state_dir / "healthcheck_state.json"
    state: Dict[str, Dict] = read_json(state_file, default={}) or {}
    now = time.time()

    problems, status = run_checks(cfg)
    active = {key for key, _, _ in problems}

    for key, severity, message in problems:
        entry = state.get(key)
        if entry is None:
            state[key] = {"since": now, "last_alert": now, "severity": severity}
            send_alert(cfg, severity, message)
        elif now - entry["last_alert"] >= cfg.alert_repeat_seconds:
            entry["last_alert"] = now
            minutes = (now - entry["since"]) / 60
            send_alert(cfg, severity, f"STILL FAILING ({minutes:.0f} min): {message}")

    for key in list(state):
        if key not in active:
            minutes = (now - state[key]["since"]) / 60
            send_alert(cfg, "INFO", f"RESOLVED: '{key}' cleared after {minutes:.0f} min.")
            del state[key]

    if "service_down" in active and cfg.restart_service_if_stale:
        restart_service(cfg)

    write_json_atomic(state_file, state)

    # Human-readable health page in the shared folder.
    if cfg.publish_dir:
        summary = {
            "checked_utc": utc_now_iso(),
            "host": hostname(),
            "healthy": not problems,
            "problems": [{"check": k, "severity": s, "message": m} for k, s, m in problems],
            "service_status": status,
        }
        lines = [f"PI Mismatch Transfer - checked {summary['checked_utc']} on {summary['host']}",
                 "STATUS: OK" if not problems else "STATUS: PROBLEM", ""]
        lines += [f"[{s}] {m}" for _, s, m in problems]
        lines += ["", f"Last successful transfer: {status.get('last_success_utc')} "
                      f"({status.get('last_success_file')})",
                  f"Files waiting: {status.get('inbox_count')}"]
        try:
            write_json_atomic(cfg.publish_dir / "health.json", summary)
            (cfg.publish_dir / "HEALTH.txt").write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not publish health files: %s", exc)

    if not problems:
        log.info("Healthy. queue=%s last_success=%s",
                 status.get("inbox_count"), status.get("last_success_utc"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
