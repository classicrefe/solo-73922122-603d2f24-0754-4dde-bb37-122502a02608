"""Configuration for the laboratory inbox bridge service.

All settings are read from environment variables so the same image can run as
several independent collector instances (Docker Compose runs two of them).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:  # configuration errors should be loud
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    # Identity of this collector process. It is stored in the database lease
    # rows so that a stale scan can never advance progress belonging to a
    # newer scan or a newer mailbox generation.
    instance_id: str = field(default_factory=lambda: _env("INSTANCE_ID", "instance"))

    # SQLite database shared by both instances (a Docker named volume, or a
    # local file when running without Docker). WAL mode + busy_timeout allow
    # two collector processes plus the web UI to share one database file.
    database_path: Path = field(
        default_factory=lambda: Path(_env("DATABASE_PATH", "/data/bridge.sqlite3"))
    )

    # Where immutable attachment originals and receipts are persisted before
    # any message is marked \Seen.
    storage_dir: Path = field(
        default_factory=lambda: Path(_env("STORAGE_DIR", "/data/store"))
    )

    # Mailbox connection. In Docker Compose this is the bundled demo IMAP
    # server; in production it points at the laboratory's mail server.
    imap_host: str = field(default_factory=lambda: _env("IMAP_HOST", "127.0.0.1"))
    imap_port: int = field(default_factory=lambda: _env_int("IMAP_PORT", 1143))
    # Logical account name used as part of the database key. IMAP servers do
    # not expose a stable account identifier, so deployment configures it.
    account: str = field(
        default_factory=lambda: _env("IMAP_ACCOUNT", "lab@imap-demo")
    )
    imap_user: str = field(default_factory=lambda: _env("IMAP_USER", "lab"))
    imap_password: str = field(default_factory=lambda: _env("IMAP_PASSWORD", "lab-pass"))
    imap_mailbox: str = field(default_factory=lambda: _env("IMAP_MAILBOX", "INBOX"))
    imap_timeout: float = field(default_factory=lambda: float(_env("IMAP_TIMEOUT", "20")))

    # Duration of the exclusive scan lease. A scan refreshes it for every
    # processed message; a crashed scan blocks the other instance only until
    # the lease expires.
    scan_lease_seconds: float = field(
        default_factory=lambda: float(_env("SCAN_LEASE_SECONDS", "60"))
    )

    # Collection always searches ALL messages in the mailbox, never only
    # UNSEEN: a person may open (mark read) a message before the collector
    # reaches it, and that must not cause data loss.  \Seen is only a delivery
    # acknowledgement written after persistence.
    max_attachment_bytes: int = field(
        default_factory=lambda: _env_int("MAX_ATTACHMENT_BYTES", 256 * 1024)
    )
    max_attachments_per_mail: int = field(
        default_factory=lambda: _env_int("MAX_ATTACHMENTS_PER_MAIL", 3)
    )

    # Number of mail cycles between automatic IMAP scans. 0 disables the loop,
    # leaving only explicit scans started from the web page / API.
    auto_scan_interval: float = field(
        default_factory=lambda: float(_env("AUTO_SCAN_INTERVAL", "15"))
    )

    # UI presentation only.
    title: str = field(default_factory=lambda: _env("APP_TITLE", "检验附件入仓桥"))


settings = Settings()
