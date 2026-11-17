"""SQLite persistence and cross-instance coordination.

The same database file is shared by two collector instances and the web UI.
SQLite in WAL mode with a busy timeout supports that; the scan lease stored in
``mailbox_state`` keeps only one collector writing a given mailbox at a time.

Two guards make stale scans harmless:

* every scan owns a random ``scan_token``; progress updates, marks and lease
  clearing are conditional on that token still being current;
* the token is bound to the UIDVALIDITY captured by SELECT.  A mailbox rebuild
  resets the generation, clears the lease and marks old pending deliveries as
  stale, so a scan from before the rebuild can neither advance progress nor
  mark a message in the rebuilt mailbox.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 2


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(self.path)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mailbox_state (
                    account TEXT NOT NULL,
                    mailbox TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL DEFAULT 0,
                    generation INTEGER NOT NULL DEFAULT 0,
                    last_uid INTEGER NOT NULL DEFAULT 0,
                    scan_token TEXT,
                    scan_instance TEXT,
                    scan_uidvalidity INTEGER,
                    scan_started_at REAL,
                    scan_heartbeat_at REAL,
                    scan_lease_seconds REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account, mailbox)
                );

                CREATE TABLE IF NOT EXISTS scan_run (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT NOT NULL UNIQUE,
                    account TEXT NOT NULL,
                    mailbox TEXT NOT NULL,
                    instance TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    scanned INTEGER NOT NULL DEFAULT 0,
                    accepted INTEGER NOT NULL DEFAULT 0,
                    rejected INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    reused_reports INTEGER NOT NULL DEFAULT 0,
                    marked INTEGER NOT NULL DEFAULT 0,
                    last_uid INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS message_record (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL,
                    mailbox TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    raw_sha256 TEXT NOT NULL,
                    subject TEXT,
                    from_addr TEXT,
                    date_header TEXT,
                    decision TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected')),
                    reason_code TEXT,
                    reason TEXT,
                    mark_state TEXT NOT NULL DEFAULT 'pending'
                        CHECK (mark_state IN ('pending', 'marked', 'stale')),
                    mark_attempts INTEGER NOT NULL DEFAULT 0,
                    marked_at TEXT,
                    receipt_sha256 TEXT NOT NULL,
                    receipt_path TEXT NOT NULL,
                    original_path TEXT NOT NULL,
                    report_nos TEXT NOT NULL DEFAULT '[]',
                    detail TEXT,
                    scan_instance TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (account, mailbox, uidvalidity, uid)
                );

                CREATE TABLE IF NOT EXISTS report (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL,
                    mailbox TEXT NOT NULL,
                    report_no TEXT NOT NULL,
                    sample_no TEXT NOT NULL,
                    item TEXT NOT NULL,
                    value TEXT NOT NULL,
                    rows_json TEXT NOT NULL,
                    attachment_sha256 TEXT NOT NULL,
                    attachment_name TEXT NOT NULL,
                    attachment_path TEXT NOT NULL,
                    source_message_id INTEGER NOT NULL REFERENCES message_record(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (account, mailbox, report_no)
                );

                CREATE TABLE IF NOT EXISTS delivery (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL REFERENCES message_record(id),
                    report_no TEXT NOT NULL,
                    attachment_sha256 TEXT NOT NULL,
                    attachment_name TEXT NOT NULL,
                    attachment_path TEXT NOT NULL,
                    disposition TEXT NOT NULL CHECK (disposition IN ('new', 'reused')),
                    created_at TEXT NOT NULL,
                    UNIQUE (message_id, report_no)
                );

                CREATE INDEX IF NOT EXISTS idx_message_created
                    ON message_record(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_message_mark
                    ON message_record(account, mailbox, mark_state);
                CREATE INDEX IF NOT EXISTS idx_report_no
                    ON report(account, mailbox, report_no);
                CREATE INDEX IF NOT EXISTS idx_delivery_message
                    ON delivery(message_id);
                CREATE INDEX IF NOT EXISTS idx_scan_started
                    ON scan_run(started_at DESC);
                """
            )
            # Small forward-only migrations for databases created by an
            # earlier build of this service.
            scan_cols = {row[1] for row in conn.execute("PRAGMA table_info(scan_run)")}
            if "marked" not in scan_cols:
                conn.execute(
                    "ALTER TABLE scan_run ADD COLUMN marked INTEGER NOT NULL DEFAULT 0"
                )
            state_cols = {row[1] for row in conn.execute("PRAGMA table_info(mailbox_state)")}
            if "scan_lease_seconds" not in state_cols:
                conn.execute(
                    "ALTER TABLE mailbox_state"
                    " ADD COLUMN scan_lease_seconds REAL NOT NULL DEFAULT 0"
                )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        finally:
            conn.close()

    @contextmanager
    def connect(self):
        conn = _connect(self.path)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ state
    def get_state(self, account: str, mailbox: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM mailbox_state WHERE account=? AND mailbox=?",
                (account, mailbox),
            ).fetchone()

    def claim_scan(
        self,
        account: str,
        mailbox: str,
        server_uidvalidity: int,
        instance: str,
        lease_seconds: float,
    ) -> tuple[bool, str | None, int]:
        """Atomically claim the mailbox scan lease.

        Returns ``(claimed, token, generation)``.  If the server's UIDVALIDITY
        differs from the stored one, the mailbox generation is reset first and
        every live lease from the old generation is revoked.  Pending marks
        from the old generation become ``stale`` and will never be applied to
        messages that reuse their UIDs in the rebuilt mailbox.
        """
        token = uuid.uuid4().hex
        now = time.time()
        with self.connect() as conn:
            with transaction(conn):
                row = conn.execute(
                    "SELECT * FROM mailbox_state WHERE account=? AND mailbox=?",
                    (account, mailbox),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """INSERT INTO mailbox_state
                           (account, mailbox, uidvalidity, generation, last_uid,
                            updated_at)
                           VALUES (?, ?, ?, 1, 0, ?)""",
                        (account, mailbox, server_uidvalidity, utcnow()),
                    )
                    generation = 1
                else:
                    generation = row["generation"]
                    if row["uidvalidity"] != server_uidvalidity:
                        generation += 1
                        # Invalidate any scan belonging to the old mailbox and
                        # quarantine its pending marks.
                        conn.execute(
                            """UPDATE message_record SET mark_state='stale',
                                   updated_at=?
                               WHERE account=? AND mailbox=? AND uidvalidity<>?
                                 AND mark_state='pending'""",
                            (utcnow(), account, mailbox, server_uidvalidity),
                        )
                        conn.execute(
                            """UPDATE mailbox_state
                               SET uidvalidity=?, generation=?, last_uid=0,
                                   scan_token=NULL, scan_instance=NULL,
                                   scan_uidvalidity=NULL, scan_started_at=NULL,
                                   scan_heartbeat_at=NULL, updated_at=?
                               WHERE account=? AND mailbox=?""",
                            (
                                server_uidvalidity,
                                generation,
                                utcnow(),
                                account,
                                mailbox,
                            ),
                        )
                        row = conn.execute(
                            "SELECT * FROM mailbox_state WHERE account=? AND mailbox=?",
                            (account, mailbox),
                        ).fetchone()

                row = conn.execute(
                    "SELECT * FROM mailbox_state WHERE account=? AND mailbox=?",
                    (account, mailbox),
                ).fetchone()
                # Lease expiry uses the duration stored when the lease was
                # granted: the instance asking now does not know how long the
                # other instance's scan was supposed to live.
                active = (
                    row["scan_token"] is not None
                    and row["scan_heartbeat_at"] is not None
                    and now - row["scan_heartbeat_at"] < (row["scan_lease_seconds"] or 0)
                    and row["scan_uidvalidity"] == server_uidvalidity
                )
                # An unexpired lease always belongs to a live scan, whether it
                # runs in this process or the other instance.  Taking it over
                # would let two scans interleave, so both are refused.
                if active:
                    return False, None, int(row["generation"])

                conn.execute(
                    """UPDATE mailbox_state
                       SET scan_token=?, scan_instance=?, scan_uidvalidity=?,
                           scan_started_at=?, scan_heartbeat_at=?,
                           scan_lease_seconds=?, updated_at=?
                       WHERE account=? AND mailbox=?""",
                    (
                        token,
                        instance,
                        server_uidvalidity,
                        now,
                        now,
                        lease_seconds,
                        utcnow(),
                        account,
                        mailbox,
                    ),
                )
                conn.execute(
                    """INSERT INTO scan_run
                       (token, account, mailbox, instance, uidvalidity, status,
                        started_at)
                       VALUES (?, ?, ?, ?, ?, 'running', ?)""",
                    (token, account, mailbox, instance, server_uidvalidity, utcnow()),
                )
                return True, token, generation

    def token_is_current(self, token: str, uidvalidity: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT 1 FROM mailbox_state
                   WHERE scan_token=? AND uidvalidity=? AND scan_uidvalidity=?""",
                (token, uidvalidity, uidvalidity),
            ).fetchone()
            return row is not None

    def set_progress(self, token: str, uidvalidity: int, last_uid: int) -> bool:
        """Advance progress only if this scan still owns the current mailbox."""
        with self.connect() as conn:
            with transaction(conn):
                cur = conn.execute(
                    """UPDATE mailbox_state
                       SET last_uid=CASE WHEN last_uid > ? THEN last_uid ELSE ? END,
                           scan_heartbeat_at=?, updated_at=?
                       WHERE scan_token=? AND uidvalidity=? AND scan_uidvalidity=?""",
                    (
                        last_uid,
                        last_uid,
                        time.time(),
                        utcnow(),
                        token,
                        uidvalidity,
                        uidvalidity,
                    ),
                )
                if cur.rowcount == 1:
                    conn.execute(
                        "UPDATE scan_run SET last_uid=? WHERE token=?",
                        (last_uid, token),
                    )
                return cur.rowcount == 1

    def finish_scan(
        self,
        token: str,
        status: str,
        counts: dict[str, int] | None = None,
        error: str | None = None,
    ) -> None:
        counts = counts or {}
        with self.connect() as conn:
            with transaction(conn):
                conn.execute(
                    """UPDATE scan_run
                       SET status=?, finished_at=?, scanned=?, accepted=?,
                           rejected=?, skipped=?, reused_reports=?, marked=?,
                           error=?
                       WHERE token=?""",
                    (
                        status,
                        utcnow(),
                        counts.get("scanned", 0),
                        counts.get("accepted", 0),
                        counts.get("rejected", 0),
                        counts.get("skipped", 0),
                        counts.get("reused_reports", 0),
                        counts.get("marked", 0),
                        error,
                        token,
                    ),
                )
                conn.execute(
                    """UPDATE mailbox_state
                       SET scan_token=NULL, scan_instance=NULL,
                           scan_uidvalidity=NULL, scan_started_at=NULL,
                           scan_heartbeat_at=NULL, updated_at=?
                       WHERE scan_token=?""",
                    (utcnow(), token),
                )

    def adopt_changed_uidvalidity(
        self,
        account: str,
        mailbox: str,
        expected_uidvalidity: int,
        observed_uidvalidity: int,
        token: str,
    ) -> bool:
        """Record a UIDVALIDITY change observed by a running scan.

        Applies only while the caller's scan still owns the lease for the
        mailbox generation it thought it was scanning.  If any other scan has
        since claimed the mailbox, or a newer generation was already adopted,
        nothing happens: a late observer can never roll back progress that a
        newer scan made.
        """
        with self.connect() as conn:
            with transaction(conn):
                row = conn.execute(
                    "SELECT * FROM mailbox_state WHERE account=? AND mailbox=?",
                    (account, mailbox),
                ).fetchone()
                if (
                    row is None
                    or row["uidvalidity"] != expected_uidvalidity
                    or row["scan_token"] != token
                    or row["scan_uidvalidity"] != expected_uidvalidity
                    or observed_uidvalidity == expected_uidvalidity
                ):
                    return False
                conn.execute(
                    """UPDATE message_record SET mark_state='stale', updated_at=?
                       WHERE account=? AND mailbox=? AND uidvalidity<>?
                         AND mark_state='pending'""",
                    (utcnow(), account, mailbox, observed_uidvalidity),
                )
                conn.execute(
                    """UPDATE mailbox_state
                       SET uidvalidity=?, generation=generation+1, last_uid=0,
                           scan_token=NULL, scan_instance=NULL,
                           scan_uidvalidity=NULL, scan_started_at=NULL,
                           scan_heartbeat_at=NULL, updated_at=?
                       WHERE account=? AND mailbox=? AND uidvalidity=?
                         AND scan_token=?""",
                    (
                        observed_uidvalidity,
                        utcnow(),
                        account,
                        mailbox,
                        expected_uidvalidity,
                        token,
                    ),
                )
                return True

    # --------------------------------------------------------------- messages
    def message_for_uid(self, account: str, mailbox: str, uidvalidity: int, uid: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM message_record
                   WHERE account=? AND mailbox=? AND uidvalidity=? AND uid=?""",
                (account, mailbox, uidvalidity, uid),
            ).fetchone()

    def get_message(self, message_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM message_record WHERE id=?", (message_id,)
            ).fetchone()

    def list_messages(self, limit: int = 100, mark_state: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM message_record"
        params: list[Any] = []
        if mark_state:
            sql += " WHERE mark_state=?"
            params.append(mark_state)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def pending_messages(
        self,
        account: str,
        mailbox: str,
        uidvalidity: int | None = None,
    ) -> list[sqlite3.Row]:
        """Rows still waiting for the \\Seen acknowledgement.

        Newest first: a fresh message is the most likely one to be pending, and
        its UID is the least likely to have been reused.
        """
        sql = (
            "SELECT * FROM message_record WHERE mark_state='pending'"
            " AND account=? AND mailbox=?"
        )
        params: list[Any] = [account, mailbox]
        if uidvalidity is not None:
            sql += " AND uidvalidity=?"
            params.append(uidvalidity)
        sql += " ORDER BY id DESC"
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def reports_for_message(self, message_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT d.report_no, d.attachment_sha256, d.attachment_name,
                          d.attachment_path, d.disposition, d.created_at,
                          r.id AS report_id, r.sample_no, r.item, r.value
                   FROM delivery d
                   LEFT JOIN report r
                     ON r.account=(SELECT account FROM message_record WHERE id=d.message_id)
                    AND r.mailbox=(SELECT mailbox FROM message_record WHERE id=d.message_id)
                    AND r.report_no=d.report_no
                   WHERE d.message_id=?
                   ORDER BY d.report_no""",
                (message_id,),
            ).fetchall()

    def get_report(self, report_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM report WHERE id=?", (report_id,)).fetchone()

    def list_reports(self, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM report ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def set_mark_state(self, message_id: int, state: str) -> None:
        with self.connect() as conn:
            with transaction(conn):
                conn.execute(
                    """UPDATE message_record
                       SET mark_state=?, mark_attempts=mark_attempts+1,
                           marked_at=CASE WHEN ?='marked' THEN ? ELSE marked_at END,
                           updated_at=?
                       WHERE id=?""",
                    (state, state, utcnow(), utcnow(), message_id),
                )

    def increment_mark_attempts(self, message_id: int) -> None:
        with self.connect() as conn:
            with transaction(conn):
                conn.execute(
                    "UPDATE message_record SET mark_attempts=mark_attempts+1, updated_at=? WHERE id=?",
                    (utcnow(), message_id),
                )

    # --------------------------------------------------------------- snapshot
    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            def one(sql: str, params: Iterable[Any] = ()) -> int:
                row = conn.execute(sql, tuple(params)).fetchone()
                return int(row[0]) if row else 0

            return {
                "messages": one("SELECT COUNT(*) FROM message_record"),
                "accepted": one("SELECT COUNT(*) FROM message_record WHERE decision='accepted'"),
                "rejected": one("SELECT COUNT(*) FROM message_record WHERE decision='rejected'"),
                "pending_marks": one("SELECT COUNT(*) FROM message_record WHERE mark_state='pending'"),
                "marked": one("SELECT COUNT(*) FROM message_record WHERE mark_state='marked'"),
                "stale_marks": one("SELECT COUNT(*) FROM message_record WHERE mark_state='stale'"),
                "reports": one("SELECT COUNT(*) FROM report"),
                "scans": one("SELECT COUNT(*) FROM scan_run"),
            }

    def recent_scans(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM scan_run ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def active_scan(self) -> sqlite3.Row | None:
        """The lease holder, regardless of which instance owns it."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM mailbox_state WHERE scan_token IS NOT NULL LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            return conn.execute(
                """SELECT * FROM scan_run
                   WHERE token=? ORDER BY id DESC LIMIT 1""",
                (row["scan_token"],),
            ).fetchone()


__all__ = ["Database", "transaction", "utcnow"]
