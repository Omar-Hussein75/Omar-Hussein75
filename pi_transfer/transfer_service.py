r"""
PI Mismatch transfer service.

Sends every *.json dropped into the inbox to the remote SFTP server,
with nothing lost across crashes, reboots, network outages or a
missing archive drive.

The existing folders keep exactly the same behaviour as the old script:

    inbox_dir (C:\PI_MISMATCH_TOOL)   files stay here until they are confirmed
                                      on the remote - "gone from inbox" still
                                      means "sent"
    remote_dir                        receives <name>.json, nothing else
    archive_dir (...\Out)             receives the sent file; on a name clash
                                      <stem>_<YYYYmmdd_HHMMSS>.json, as before

Everything new lives in state_dir (C:\PI_MISMATCH_TOOL_STATE):

    state_dir\uploaded\    sent, but the archive folder was unavailable;
                           moved to archive_dir as soon as it is back
    state_dir\logs\        logs + sent-YYYY-MM.jsonl audit journal
    state_dir\status.json  heartbeat read by healthcheck.py

The inbox itself is the queue: a crash at any moment leaves unsent
files in the inbox, and the next start sends them.

Usage:
    python transfer_service.py                    run forever (as a Windows service)
    python transfer_service.py --once             one pass, then exit
    python transfer_service.py --trust-host-key   save the server's SSH host key (run once)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import posixpath
import shlex
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import paramiko

from common import (
    Config,
    SingleInstanceLock,
    get_sftp_password,
    hostname,
    load_config,
    setup_logging,
    unique_path,
    utc_now_iso,
    write_json_atomic,
)

log = logging.getLogger("transfer")

STOP = threading.Event()


# ==========================================
# Retry bookkeeping
# ==========================================

@dataclass
class RetryState:
    attempts: int = 0
    next_try: float = 0.0
    last_error: str = ""
    invalid: bool = False


def backoff_delay(attempts: int, cap: float) -> float:
    """5s, 10s, 20s, 40s ... capped. Never gives up."""
    return min(cap, 5.0 * (2 ** max(0, attempts - 1)))


# ==========================================
# SFTP client with timeouts, keepalive and host-key checking
# ==========================================

class SftpUploader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client: Optional[paramiko.SSHClient] = None
        self.sftp: Optional[paramiko.SFTPClient] = None
        self.last_used = 0.0

    def close(self) -> None:
        for obj in (self.sftp, self.client):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self.sftp = None
        self.client = None

    def _is_alive(self) -> bool:
        if self.client is None or self.sftp is None:
            return False
        transport = self.client.get_transport()
        if transport is None or not transport.is_active():
            return False
        # Idle for a while: prove the session still works with a cheap call.
        if time.time() - self.last_used > 60:
            try:
                self.sftp.stat(self.cfg.remote_dir)
            except Exception:
                return False
        return True

    def ensure_connected(self) -> None:
        if self._is_alive():
            return
        self.close()

        cfg = self.cfg
        if not cfg.known_hosts_file.exists():
            raise RuntimeError(
                f"No trusted host key at {cfg.known_hosts_file}. "
                "Run: python transfer_service.py --trust-host-key"
            )

        client = paramiko.SSHClient()
        client.load_host_keys(str(cfg.known_hosts_file))
        # Refuse unknown/changed host keys (protects against MITM / wrong server).
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

        password = None if cfg.sftp_key_file else get_sftp_password(cfg)
        if not cfg.sftp_key_file and not password:
            raise RuntimeError(
                "No SFTP credentials: set [sftp] key_file, or store the password "
                "in Windows Credential Manager / PI_SFTP_PASSWORD."
            )

        log.info("Connecting to %s:%s ...", cfg.sftp_host, cfg.sftp_port)
        client.connect(
            hostname=cfg.sftp_host,
            port=cfg.sftp_port,
            username=cfg.sftp_username,
            password=password,
            key_filename=cfg.sftp_key_file,
            timeout=cfg.connect_timeout_seconds,
            banner_timeout=cfg.connect_timeout_seconds,
            auth_timeout=cfg.connect_timeout_seconds,
            allow_agent=False,
            look_for_keys=False,
        )
        transport = client.get_transport()
        assert transport is not None
        transport.set_keepalive(30)  # detect dead links / keep firewalls from dropping us

        sftp = client.open_sftp()
        # Any single read/write blocking longer than this raises instead of hanging forever.
        sftp.get_channel().settimeout(cfg.io_timeout_seconds)
        sftp.stat(cfg.remote_dir)  # fails fast if the remote folder is missing

        self.client, self.sftp = client, sftp
        self.last_used = time.time()
        log.info("Connected.")

    def upload(self, local: Path, local_sha256: str) -> str:
        """
        Default: upload straight to remote_dir/<name>, exactly like the old
        script, then verify. If remote_staging_dir is configured (a folder
        you create on the same remote filesystem), upload there first and
        rename into place, so the consumer can never see a partial file.
        """
        assert self.sftp is not None
        cfg = self.cfg
        final_path = posixpath.join(cfg.remote_dir, local.name)
        upload_path = (posixpath.join(cfg.remote_staging_dir, local.name)
                       if cfg.remote_staging_dir else final_path)
        local_size = local.stat().st_size

        # confirm=True makes paramiko stat the remote file and raise on size mismatch.
        self.sftp.put(str(local), upload_path, confirm=True)

        remote_size = self.sftp.stat(upload_path).st_size
        if remote_size != local_size:
            raise IOError(f"Size mismatch after upload: local {local_size}, remote {remote_size}")

        if cfg.verify_remote_sha256:
            remote_hash = self._remote_sha256(upload_path)
            if remote_hash != local_sha256:
                raise IOError(f"SHA-256 mismatch: local {local_sha256}, remote {remote_hash}")

        if upload_path != final_path:
            try:
                self.sftp.posix_rename(upload_path, final_path)  # atomic overwrite
            except IOError:
                try:
                    self.sftp.remove(final_path)
                except IOError:
                    pass
                self.sftp.rename(upload_path, final_path)
            if self.sftp.stat(final_path).st_size != local_size:
                raise IOError("Remote file size changed after rename")

        self.last_used = time.time()
        return final_path

    def _remote_sha256(self, remote_path: str) -> str:
        assert self.client is not None
        _, stdout, stderr = self.client.exec_command(
            f"sha256sum -- {shlex.quote(remote_path)}",
            timeout=self.cfg.io_timeout_seconds,
        )
        out = stdout.read().decode("utf-8", "replace").strip()
        if stdout.channel.recv_exit_status() != 0 or not out:
            raise IOError(f"Remote sha256sum failed: {stderr.read().decode('utf-8', 'replace')}")
        return out.split()[0].lower()


# ==========================================
# The service
# ==========================================

class TransferService:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.uploader = SftpUploader(cfg)
        self.retries: Dict[str, RetryState] = {}
        self.inbox_seen: Dict[str, Tuple[int, float]] = {}  # name -> (size, mtime) at last scan
        self.connect_failures = 0
        self.next_connect_try = 0.0
        self.connection_ok: Optional[bool] = None
        self.last_error = ""
        self.last_error_time: Optional[str] = None
        self.last_success_time: Optional[str] = None
        self.last_success_file: Optional[str] = None
        self.sent_since_start = 0
        self.archive_error = ""
        self.started = utc_now_iso()
        self.last_tick = time.time()

    def ensure_dirs(self) -> None:
        # Only the NEW folders are created here. The archive folder is
        # created on demand, exactly as the old script did.
        for d in (self.cfg.uploaded_dir, self.cfg.log_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---- which inbox files are ready ----

    def ready_files(self) -> List[Path]:
        """
        Inbox files the producer has finished writing, oldest first.
        Nothing in the inbox is renamed, moved or modified here.
        """
        cfg = self.cfg
        if not cfg.inbox_dir.exists():
            raise FileNotFoundError(f"Inbox folder does not exist: {cfg.inbox_dir}")

        now = time.time()
        seen_now: Dict[str, Tuple[int, float]] = {}
        ready: List[Tuple[float, Path]] = []

        for path in cfg.inbox_dir.glob("*.json"):
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            sig = (st.st_size, st.st_mtime)
            seen_now[path.name] = sig

            # Still being written? Require: unchanged since the previous scan
            # AND untouched for settle_seconds. (Copies keep old mtimes, so the
            # unchanged-between-scans test matters too.)
            if self.inbox_seen.get(path.name) != sig:
                continue
            if now - st.st_mtime < cfg.settle_seconds:
                continue
            if not writer_finished(path):
                continue
            ready.append((st.st_mtime, path))

        self.inbox_seen = seen_now
        # Forget retry state for files that have left the inbox.
        for name in list(self.retries):
            if name not in seen_now:
                del self.retries[name]

        ready.sort(key=lambda item: item[0])  # oldest first: nothing starves
        return [p for _, p in ready]

    def validate(self, path: Path) -> Optional[str]:
        if not self.cfg.validate_json:
            return None
        try:
            # bytes input: json auto-detects UTF-8 / UTF-8-BOM / UTF-16.
            json.loads(path.read_bytes())
            return None
        except ValueError as exc:
            return f"Invalid JSON: {exc}"

    # ---- upload ----

    def upload_ready(self) -> None:
        cfg = self.cfg
        files = self.ready_files()
        now = time.time()
        due = [p for p in files
               if self.retries.get(p.name, RetryState()).next_try <= now]
        if not due or now < self.next_connect_try:
            return

        for path in due:
            if STOP.is_set():
                return
            self.last_tick = time.time()

            # Invalid JSON is NOT sent and NOT moved: it stays in the inbox,
            # is re-checked with backoff (the producer may rewrite it), and
            # the healthcheck raises an alert.
            problem = self.validate(path)
            if problem:
                self._file_failed(path, problem, invalid=True)
                continue

            try:
                self.uploader.ensure_connected()
                if self.connection_ok is not True:
                    log.info("SFTP connection healthy.")
                self.connection_ok = True
                self.connect_failures = 0
            except Exception as exc:
                # Every file would fail: back off globally instead of per file.
                self.uploader.close()
                self.connection_ok = False
                self.connect_failures += 1
                delay = backoff_delay(self.connect_failures, cfg.max_retry_delay_seconds)
                self.next_connect_try = time.time() + delay
                self._record_error(f"Connection failed: {type(exc).__name__}: {exc}")
                log.error("SFTP connection failed (%s). Retrying in %.0fs.", exc, delay)
                return

            try:
                sha = sha256_of(path)
                remote_path = self.uploader.upload(path, sha)
            except Exception as exc:
                self.uploader.close()  # connection may be in a bad state
                self._file_failed(path, f"{type(exc).__name__}: {exc}")
                continue

            # Confirmed on the remote: record it, then take it out of the inbox.
            self._journal(path, sha, remote_path)
            self.retries.pop(path.name, None)
            self.sent_since_start += 1
            self.last_success_time = utc_now_iso()
            self.last_success_file = path.name
            log.info("SENT %s -> %s", path.name, remote_path)
            self._move_out_of_inbox(path)

    def _move_out_of_inbox(self, path: Path) -> None:
        """inbox -> archive (as before); if the archive is unavailable,
        inbox -> state\\uploaded so the file is never uploaded twice."""
        try:
            self.cfg.archive_dir.mkdir(parents=True, exist_ok=True)
            dest = unique_path(self.cfg.archive_dir, path.name)
            shutil.move(str(path), str(dest))
            return
        except OSError as exc:
            self._archive_failed(exc)
        try:
            os.replace(path, unique_path(self.cfg.uploaded_dir, path.name))
        except OSError as exc:
            # Extremely unlikely (same drive). Worst case: the file is sent
            # again next cycle - a duplicate, never a loss.
            log.error("Could not move sent file %s out of the inbox: %s", path.name, exc)

    def _file_failed(self, path: Path, error: str, invalid: bool = False) -> None:
        state = self.retries.setdefault(path.name, RetryState())
        state.attempts += 1
        state.last_error = error
        state.invalid = invalid
        delay = backoff_delay(state.attempts, self.cfg.max_retry_delay_seconds)
        state.next_try = time.time() + delay
        self._record_error(f"{path.name}: {error}")
        log.warning("%s failed (attempt %d): %s. Retrying in %.0fs.",
                    path.name, state.attempts, error, delay)

    def _record_error(self, text: str) -> None:
        self.last_error = text
        self.last_error_time = utc_now_iso()

    def _journal(self, path: Path, sha: str, remote_path: str) -> None:
        """Append-only audit trail: answers 'was my file sent, and when?'"""
        record = {
            "time_utc": utc_now_iso(),
            "file": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha,
            "remote": remote_path,
            "host": hostname(),
        }
        journal = self.cfg.log_dir / f"sent-{time.strftime('%Y-%m')}.jsonl"
        try:
            with open(journal, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            log.warning("Could not write journal: %s", exc)

    # ---- archive catch-up (an unavailable archive never blocks uploads) ----

    def archive_backlog(self) -> None:
        files = list(self.cfg.uploaded_dir.glob("*.json"))
        if not files:
            self.archive_error = ""
            return
        try:
            self.cfg.archive_dir.mkdir(parents=True, exist_ok=True)
            for path in files:
                shutil.move(str(path), str(unique_path(self.cfg.archive_dir, path.name)))
            log.info("Archive folder reachable again; %d file(s) archived.", len(files))
            self.archive_error = ""
        except OSError as exc:
            self._archive_failed(exc)

    def _archive_failed(self, exc: Exception) -> None:
        if not self.archive_error:
            log.warning("Archiving to %s failed (will retry): %s", self.cfg.archive_dir, exc)
        self.archive_error = str(exc)

    # ---- status / heartbeat ----

    def write_status(self) -> None:
        cfg = self.cfg
        now = time.time()

        def ages(folder: Path) -> List[float]:
            out = []
            if folder.exists():
                for p in folder.glob("*.json"):
                    try:
                        out.append(now - p.stat().st_mtime)
                    except FileNotFoundError:
                        pass
            return out

        inbox_ages = ages(cfg.inbox_dir)
        status = {
            "service": "PI Mismatch Transfer",
            "host": hostname(),
            "pid": os.getpid(),
            "started_utc": self.started,
            "heartbeat_utc": utc_now_iso(),
            "connection_ok": self.connection_ok,
            "last_success_utc": self.last_success_time,
            "last_success_file": self.last_success_file,
            "sent_since_start": self.sent_since_start,
            "inbox_count": len(inbox_ages),
            "inbox_oldest_age_seconds": round(max(inbox_ages, default=0)),
            "awaiting_archive_count": len(ages(cfg.uploaded_dir)),
            "archive_error": self.archive_error or None,
            "invalid_files": sorted(n for n, s in self.retries.items() if s.invalid),
            "last_error": self.last_error or None,
            "last_error_utc": self.last_error_time,
            "retrying": [
                {"file": n, "attempts": s.attempts, "last_error": s.last_error}
                for n, s in sorted(self.retries.items())
            ],
        }
        write_json_atomic(cfg.status_file, status)

    # ---- main loop ----

    def run_cycle(self) -> None:
        self.last_tick = time.time()
        for stage in (self.upload_ready, self.archive_backlog):
            try:
                stage()
            except Exception as exc:
                # One stage failing must never stop the others or the loop.
                self._record_error(f"{stage.__name__}: {type(exc).__name__}: {exc}")
                log.exception("Error in %s", stage.__name__)
        try:
            self.write_status()
        except Exception:
            log.exception("Could not write status file")

    def run_forever(self) -> None:
        start_hang_watchdog(self)
        while not STOP.is_set():
            self.run_cycle()
            STOP.wait(self.cfg.poll_seconds)
        self.uploader.close()
        log.info("Stopped cleanly.")


def writer_finished(path: Path) -> bool:
    """
    True if no other process holds the file open for writing. Opening for
    read+write (without writing anything) fails on Windows while the
    producer still has it open. The file content and timestamps are not
    changed.
    """
    if not os.access(path, os.W_OK):
        return True  # read-only file: nobody can be writing it
    try:
        with open(path, "r+b"):
            pass
        return True
    except OSError:
        return False


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def start_hang_watchdog(service: TransferService) -> None:
    """
    Last line of defence: if the main loop stops making progress (some
    call hangs despite the timeouts), exit hard. The Windows service
    manager then restarts us with a clean process.
    """
    limit = service.cfg.hang_timeout_seconds

    def watch() -> None:
        while not STOP.wait(30):
            stalled = time.time() - service.last_tick
            if stalled > limit:
                log.critical("Main loop stalled for %.0fs - exiting so the service restarts.",
                             stalled)
                logging.shutdown()
                os._exit(2)

    threading.Thread(target=watch, name="hang-watchdog", daemon=True).start()


def trust_host_key(cfg: Config) -> None:
    """Fetch the server's host key, show its fingerprint and save it."""
    transport = paramiko.Transport((cfg.sftp_host, cfg.sftp_port))
    transport.banner_timeout = cfg.connect_timeout_seconds
    try:
        transport.start_client(timeout=cfg.connect_timeout_seconds)
        key = transport.get_remote_server_key()
    finally:
        transport.close()

    fp = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
    print(f"Server {cfg.sftp_host}:{cfg.sftp_port} presents {key.get_name()} key")
    print(f"  SHA256:{fp}")
    print("Confirm this fingerprint with the server admin (ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub).")

    hosts = paramiko.HostKeys()
    if cfg.known_hosts_file.exists():
        hosts.load(str(cfg.known_hosts_file))
    entry = cfg.sftp_host if cfg.sftp_port == 22 else f"[{cfg.sftp_host}]:{cfg.sftp_port}"
    hosts.add(entry, key.get_name(), key)
    cfg.known_hosts_file.parent.mkdir(parents=True, exist_ok=True)
    hosts.save(str(cfg.known_hosts_file))
    print(f"Saved to {cfg.known_hosts_file}")


def install_signal_handlers() -> None:
    def handler(signum, frame):  # noqa: ARG001
        log.info("Received signal %s, stopping...", signum)
        STOP.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK: Windows Ctrl+Break
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, handler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    parser.add_argument("--trust-host-key", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cfg = load_config()

    if args.trust_host_key:
        trust_host_key(cfg)
        return 0

    setup_logging(cfg, "transfer", args.verbose)

    lock = SingleInstanceLock(cfg.state_dir / "transfer.lock")
    if not lock.acquire():
        log.warning("Another instance is already running; exiting.")
        return 3

    service = TransferService(cfg)
    service.ensure_dirs()
    install_signal_handlers()
    log.info("PI Mismatch transfer service starting. inbox=%s remote=%s:%s",
             cfg.inbox_dir, cfg.sftp_host, cfg.remote_dir)

    if args.once:
        # Files only count as ready once seen unchanged across two scans.
        service.ready_files()
        time.sleep(max(1.0, cfg.settle_seconds))
        service.run_cycle()
        service.uploader.close()
        return 0

    service.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
