"""
Shared configuration, logging and file helpers for the PI Mismatch
transfer service (transfer_service.py) and its watchdog (healthcheck.py).

All settings live in config.ini next to this file (copy config.example.ini).
Secrets are NEVER read from config.ini: see get_sftp_password().
"""

from __future__ import annotations

import configparser
import json
import logging
import logging.handlers
import os
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.getenv("PI_TRANSFER_CONFIG", BASE_DIR / "config.ini"))


# ==========================================
# Configuration
# ==========================================

@dataclass(frozen=True)
class Config:
    # Folders
    inbox_dir: Path          # where producers drop *.json
    state_dir: Path          # NEW folder: logs, status, archive backlog (same drive as inbox_dir)
    archive_dir: Path        # long-term archive of sent files
    publish_dir: Optional[Path]  # optional, off by default: health files copied to a NEW folder of your choice

    # Timing
    poll_seconds: float
    settle_seconds: float    # file must be untouched this long before it is claimed
    max_retry_delay_seconds: float
    hang_timeout_seconds: float

    # SFTP
    sftp_host: str
    sftp_port: int
    sftp_username: str
    sftp_key_file: Optional[str]
    remote_dir: str
    remote_staging_dir: Optional[str]
    connect_timeout_seconds: float
    io_timeout_seconds: float
    verify_remote_sha256: bool

    # Validation
    validate_json: bool

    # Healthcheck / alerting
    service_name: str
    heartbeat_stale_seconds: float
    backlog_alert_seconds: float
    archive_backlog_alert_seconds: float
    alert_repeat_seconds: float
    restart_service_if_stale: bool
    webhook_url: str
    webhook_kind: str
    event_log_source: str

    # Derived paths
    @property
    def uploaded_dir(self) -> Path:
        return self.state_dir / "uploaded"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def status_file(self) -> Path:
        return self.state_dir / "status.json"

    @property
    def known_hosts_file(self) -> Path:
        return self.state_dir / "known_hosts"


def load_config(path: Path = CONFIG_FILE) -> Config:
    if not path.exists():
        raise SystemExit(
            f"Config file not found: {path}\n"
            f"Copy config.example.ini to config.ini and edit it."
        )

    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")

    def get(section: str, key: str, default: Optional[str] = None) -> str:
        value = cp.get(section, key, fallback=default)
        if value is None:
            raise SystemExit(f"Missing setting [{section}] {key} in {path}")
        return value.strip()

    def getf(section: str, key: str, default: str) -> float:
        return float(get(section, key, default))

    def getb(section: str, key: str, default: str) -> bool:
        return get(section, key, default).lower() in ("1", "true", "yes", "on")

    publish = get("paths", "publish_dir", "")
    key_file = get("sftp", "key_file", "")

    return Config(
        inbox_dir=Path(get("paths", "inbox_dir")),
        state_dir=Path(get("paths", "state_dir")),
        archive_dir=Path(get("paths", "archive_dir")),
        publish_dir=Path(publish) if publish else None,

        poll_seconds=getf("timing", "poll_seconds", "5"),
        settle_seconds=getf("timing", "settle_seconds", "5"),
        max_retry_delay_seconds=getf("timing", "max_retry_delay_seconds", "300"),
        hang_timeout_seconds=getf("timing", "hang_timeout_seconds", "900"),

        sftp_host=get("sftp", "host"),
        sftp_port=int(get("sftp", "port", "22")),
        sftp_username=get("sftp", "username"),
        sftp_key_file=key_file or None,
        remote_dir=get("sftp", "remote_dir").rstrip("/"),
        remote_staging_dir=get("sftp", "remote_staging_dir", "").rstrip("/") or None,
        connect_timeout_seconds=getf("sftp", "connect_timeout_seconds", "20"),
        io_timeout_seconds=getf("sftp", "io_timeout_seconds", "120"),
        verify_remote_sha256=getb("sftp", "verify_remote_sha256", "false"),

        validate_json=getb("validation", "validate_json", "true"),

        service_name=get("health", "service_name", "PITransferService"),
        heartbeat_stale_seconds=getf("health", "heartbeat_stale_seconds", "300"),
        backlog_alert_seconds=getf("health", "backlog_alert_seconds", "900"),
        archive_backlog_alert_seconds=getf("health", "archive_backlog_alert_seconds", "3600"),
        alert_repeat_seconds=getf("health", "alert_repeat_seconds", "3600"),
        restart_service_if_stale=getb("health", "restart_service_if_stale", "true"),
        webhook_url=os.getenv("PI_ALERT_WEBHOOK_URL", get("health", "webhook_url", "")),
        webhook_kind=get("health", "webhook_kind", "teams").lower(),
        event_log_source=get("health", "event_log_source", "PI_Mismatch_Transfer"),
    )


def get_sftp_password(cfg: Config) -> Optional[str]:
    """
    Password lookup order (first hit wins):
      1. Windows Credential Manager via the 'keyring' package
         (store once with:  python -m keyring set pi_mismatch_sftp <username>)
      2. Environment variable PI_SFTP_PASSWORD
    Returns None when an SSH key is used instead (recommended).
    """
    try:
        import keyring  # optional dependency

        value = keyring.get_password("pi_mismatch_sftp", cfg.sftp_username)
        if value:
            return value
    except Exception:
        pass

    return os.getenv("PI_SFTP_PASSWORD") or None


# ==========================================
# Logging
# ==========================================

def setup_logging(cfg: Config, name: str, verbose: bool = False) -> logging.Logger:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    # Daily rotation, 60 days kept.
    file_handler = logging.handlers.TimedRotatingFileHandler(
        cfg.log_dir / f"{name}.log",
        when="midnight",
        backupCount=60,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # paramiko is very chatty at INFO.
    logging.getLogger("paramiko").setLevel(logging.WARNING)

    return logging.getLogger(name)


# ==========================================
# Helpers
# ==========================================

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def hostname() -> str:
    return os.environ.get("COMPUTERNAME") or socket.gethostname()


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    # os.replace is atomic on NTFS; retry briefly in case a reader
    # (antivirus, SharePoint sync) has the target open.
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def unique_path(directory: Path, name: str) -> Path:
    """
    directory/name, or - same pattern as the old script - <stem>_<YYYYmmdd_HHMMSS><suffix>
    if that name is taken (a counter is added only if that is taken too).
    """
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = directory / f"{stem}_{stamp}{suffix}"
    if not candidate.exists():
        return candidate
    counter = 1
    while True:
        candidate = directory / f"{stem}_{stamp}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


class SingleInstanceLock:
    """
    OS-level exclusive lock. Released automatically by the OS if the
    process dies, so there are no stale locks after a crash or reboot.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True
