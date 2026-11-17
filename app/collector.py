"""IMAP collector: fetch, validate, commit to SQLite + files, then mark \\Seen.

Ordering rules implemented here:

1. A message is never marked before its attachments and receipt are durable.
   Files are fsynced and the SQLite transaction commits before any
   ``STORE +FLAGS \\Seen`` is issued.
2. Marking and ingestion are idempotent by message identity ``(account,
   mailbox, uidvalidity, uid)`` and by report number; a retry after a
   disconnect re-fetches, sees the same raw SHA-256 and does not create
   duplicate reports.  A pending-mark queue reconciles messages whose flag
   was stored but whose local state was lost.
3. A UIDVALIDITY change discovered at any point invalidates the scan token;
   no further progress is written and no further flags are stored, so an old
   scan cannot mark messages from a rebuilt mailbox that reused UIDs.
4. Progress (``last_uid``) advances only through conditional updates bound to
   the scan token and the UIDVALIDITY the scan selected.
"""

from __future__ import annotations

import hashlib
import imaplib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .db import Database, transaction, utcnow
from .mailparse import parse_mail, validate_mail
from .store import Store

log = logging.getLogger("bridge.collector")


class ScanBusy(RuntimeError):
    """Another instance currently owns the mailbox lease."""


class ScanStale(RuntimeError):
    """This scan no longer owns the mailbox (UIDVALIDITY changed / lease lost)."""


class _LinkLost(Exception):
    """The IMAP connection died; the flag may or may not have been stored."""


class _Conflict(Exception):
    """A report number already exists with a different original."""

    def __init__(self, reasons: list[dict[str, Any]]):
        super().__init__("report conflict")
        self.reasons = reasons


@dataclass
class ScanResult:
    token: str
    status: str
    uidvalidity: int
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "scanned": 0,
            "accepted": 0,
            "rejected": 0,
            "skipped": 0,
            "reused_reports": 0,
            "marked": 0,
        }
    )
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "status": self.status,
            "uidvalidity": self.uidvalidity,
            "error": self.error,
            **self.counts,
        }


def _int_response(data: list[Any]) -> int:
    if not data or data[0] is None:
        raise ScanStale("IMAP 未返回 UIDVALIDITY")
    return int(data[0].decode("ascii"))


class Collector:
    def __init__(self, settings: Settings, db: Database, store: Store):
        self.settings = settings
        self.db = db
        self.store = store

    # ------------------------------------------------------------------ IMAP
    def _connect(self) -> imaplib.IMAP4:
        conn = imaplib.IMAP4(
            self.settings.imap_host, self.settings.imap_port, timeout=self.settings.imap_timeout
        )
        try:
            conn.login(self.settings.imap_user, self.settings.imap_password)
        except Exception:
            try:
                conn.shutdown()
            except Exception:  # pragma: no cover - best effort
                pass
            raise
        return conn

    def probe(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            typ, data = conn.select(self.settings.imap_mailbox, readonly=True)
            if typ != "OK":
                raise RuntimeError(f"SELECT failed: {data!r}")
            uidvalidity = self._uidvalidity(conn)
            return {"ok": True, "uidvalidity": uidvalidity, "mailbox": self.settings.imap_mailbox}
        finally:
            try:
                conn.logout()
            except Exception:  # pragma: no cover - best effort
                pass

    def _uidvalidity(self, conn: imaplib.IMAP4) -> int:
        """Return the UIDVALIDITY advertised by the last SELECT."""
        typ, data = conn.response("UIDVALIDITY")
        if typ != "UIDVALIDITY" or not data:
            raise ScanStale("服务器未报告 UIDVALIDITY")
        return _int_response(data)

    def _list_uids(self, conn: imaplib.IMAP4) -> list[int]:
        # Always ALL.  A human may have opened a message before the collector
        # saw it; searching only UNSEEN would silently drop it.  Local
        # idempotency (uidvalidity, uid, raw SHA-256) prevents reprocessing.
        typ, data = conn.uid("SEARCH", None, "ALL")
        if typ != "OK":
            raise RuntimeError(f"UID SEARCH failed: {data!r}")
        if not data or data[0] in (None, b""):
            return []
        return sorted(int(part) for part in data[0].split())

    # ------------------------------------------------------------------ scan
    def scan(self, trigger: str = "api") -> ScanResult:
        account = self.settings.account
        mailbox = self.settings.imap_mailbox
        lease = self.settings.scan_lease_seconds
        conn = self._connect()
        token: str | None = None
        result: ScanResult | None = None
        try:
            typ, data = conn.select(mailbox)
            if typ != "OK":
                raise RuntimeError(f"SELECT {mailbox} failed: {data!r}")
            uidvalidity = self._uidvalidity(conn)

            claimed, token, _generation = self.db.claim_scan(
                account, mailbox, uidvalidity, self.settings.instance_id, lease
            )
            if not claimed or token is None:
                raise ScanBusy("另一实例正在采集该邮箱或租约尚未过期")

            result = ScanResult(token=token, status="running", uidvalidity=uidvalidity)
            log.info(
                "scan %s start instance=%s uidvalidity=%s trigger=%s",
                token,
                self.settings.instance_id,
                uidvalidity,
                trigger,
            )
            # Reconcile marks that were persisted but not locally confirmed
            # (e.g. connection dropped after STORE succeeded).
            try:
                self._mark_pending(conn, uidvalidity, token, result)
                self._fetch_and_commit(conn, uidvalidity, token, result)
                # Confirm the mailbox generation before the final mark round:
                # if the mailbox was rebuilt during the scan, this scan must
                # not mark anything or report completed progress.
                self._ensure_uidvalidity(conn, uidvalidity, token)
                self._mark_pending(conn, uidvalidity, token, result)
            except _LinkLost as exc:
                # Everything already committed stays committed; the \Seen
                # acknowledgement stays pending and is retried by the next
                # scan or by the explicit retry action.
                result.status = "interrupted"
                result.error = f"IMAP 连接中断：{exc}"
                if token:
                    self.db.finish_scan(token, "interrupted", result.counts, result.error)
                log.warning("scan %s interrupted: %s", token, exc)
                return result

            if result.status == "running":
                result.status = "completed"
            if self.db.token_is_current(token, uidvalidity):
                self.db.finish_scan(
                    token,
                    result.status,
                    result.counts,
                    result.error if result.status != "completed" else None,
                )
            else:
                result.status = "stale"
                result.error = "UIDVALIDITY 在扫描期间变化，本次扫描未推进进度、未标记新邮件"
                self.db.finish_scan(token, "stale", result.counts, result.error)
            log.info("scan %s done %s", token, result.as_dict())
            return result
        except ScanBusy:
            raise
        except ScanStale as exc:
            if token:
                try:
                    self.db.finish_scan(token, "stale", result.counts if result else None, str(exc))
                except Exception:  # pragma: no cover - best effort
                    log.exception("failed to record stale scan")
            raise
        except Exception as exc:
            if token:
                try:
                    self.db.finish_scan(token, "failed", result.counts if result else None, str(exc))
                except Exception:  # pragma: no cover - best effort
                    log.exception("failed to record scan failure")
            raise
        finally:
            try:
                conn.logout()
            except Exception:  # pragma: no cover - best effort
                pass

    def _check_token(self, token: str, uidvalidity: int) -> None:
        if not self.db.token_is_current(token, uidvalidity):
            raise ScanStale("扫描令牌已失效（可能发生了 UIDVALIDITY 变化）")

    def _ensure_uidvalidity(self, conn: imaplib.IMAP4, expected: int, token: str) -> None:
        """Re-SELECT and compare the server's UIDVALIDITY with the scan's.

        UIDVALIDITY can change at any time when the mailbox is recreated.  A
        scan that keeps going would treat reused UIDs as the messages it saw
        in the old generation and could mark or advance the new generation.
        Detecting the change here revokes the lease, quarantines pending marks
        and stops the stale scan before it writes anything else.
        """
        typ, data = conn.select(self.settings.imap_mailbox)
        if typ != "OK":
            raise RuntimeError(f"SELECT {self.settings.imap_mailbox} failed: {data!r}")
        current = self._uidvalidity(conn)
        if current != expected:
            self.db.adopt_changed_uidvalidity(
                self.settings.account,
                self.settings.imap_mailbox,
                expected,
                current,
                token,
            )
            raise ScanStale(
                f"UIDVALIDITY 由 {expected} 变为 {current}，旧扫描终止，不推进进度、不标记"
            )

    def _fetch_and_commit(
        self,
        conn: imaplib.IMAP4,
        uidvalidity: int,
        token: str,
        result: ScanResult,
    ) -> None:
        uids = self._list_uids(conn)
        for uid in uids:
            self._check_token(token, uidvalidity)
            # Re-SELECT for every message: a rebuild can happen at any time,
            # and reusing a UID from the old generation is precisely what must
            # never be committed or marked.  The extra round-trip is cheap
            # relative to FETCH.
            self._ensure_uidvalidity(conn, uidvalidity, token)
            existing = self.db.message_for_uid(
                self.settings.account, self.settings.imap_mailbox, uidvalidity, uid
            )
            if existing is not None and existing["mark_state"] in {"marked", "stale"}:
                result.counts["skipped"] += 1
                continue

            try:
                typ, data = conn.uid("FETCH", str(uid), "(UID BODY.PEEK[])")
            except (imaplib.IMAP4.abort, OSError) as exc:
                raise _LinkLost(str(exc)) from exc
            if typ != "OK":
                raise RuntimeError(f"UID FETCH {uid} failed: {data!r}")
            raw = self._extract_raw(data)
            if raw is None:
                # Message vanished between SEARCH and FETCH; nothing to do.
                result.counts["skipped"] += 1
                continue

            outcome = self._process_message(
                uidvalidity=uidvalidity,
                uid=uid,
                raw=raw,
                result=result,
            )
            if outcome == "skipped_duplicate":
                result.counts["skipped"] += 1
            elif outcome == "accepted":
                result.counts["scanned"] += 1
            elif outcome == "rejected":
                result.counts["scanned"] += 1
                result.counts["rejected"] += 1
            if not self.db.set_progress(token, uidvalidity, uid):
                raise ScanStale("扫描令牌已失效，停止推进进度")

    @staticmethod
    def _extract_raw(data: list[Any]) -> bytes | None:
        raw: bytes | None = None
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                payload = item[1]
                if isinstance(payload, bytes):
                    raw = payload
        return raw

    def _process_message(
        self,
        uidvalidity: int,
        uid: int,
        raw: bytes,
        result: ScanResult,
    ) -> str:
        account = self.settings.account
        mailbox = self.settings.imap_mailbox
        raw_sha = hashlib.sha256(raw).hexdigest()
        existing = self.db.message_for_uid(account, mailbox, uidvalidity, uid)
        if existing is not None:
            if existing["raw_sha256"] == raw_sha:
                # Already committed (accepted or rejected).  Its files and
                # receipt were fsynced before the SQLite commit, so the only
                # possible outstanding work is the \Seen acknowledgement,
                # which _mark_pending handles for every pending row.
                return "skipped_duplicate"
            # Same mailbox generation, same UID, different bytes: the server
            # violates UID immutability. Record a rejection rather than risk
            # silent data loss.
            self._commit_rejection(
                uidvalidity, uid, raw, raw_sha, None,
                [{"code": "uid_content_changed", "message": "同一 UID 的原文发生变化"}],
                existing=existing,
            )
            return "rejected"

        try:
            parsed = parse_mail(raw)
            plans, reasons = validate_mail(
                parsed,
                self.settings.max_attachment_bytes,
                self.settings.max_attachments_per_mail,
            )
        except Exception as exc:
            self._commit_rejection(
                uidvalidity, uid, raw, raw_sha, None,
                [{"code": "parse_error", "message": f"邮件无法解析：{exc}"}],
                existing=existing,
            )
            return "rejected"

        # Conflict/reuse decisions are made inside the acceptance transaction
        # below, so two instances cannot both "win" between check and insert.
        if plans is None:
            self._commit_rejection(
                uidvalidity, uid, raw, raw_sha, parsed, reasons, existing=existing
            )
            return "rejected"

        accepted = self._commit_acceptance(
            uidvalidity, uid, raw, raw_sha, parsed, plans
        )
        if "conflict" in accepted:
            self._commit_rejection(
                uidvalidity, uid, raw, raw_sha, parsed, accepted["conflict"], existing=existing
            )
            return "rejected"
        result.counts["accepted"] += 1
        result.counts["reused_reports"] += accepted["reused"]
        return "accepted"

    # ---------------------------------------------------------------- commits
    def _commit_rejection(
        self,
        uidvalidity: int,
        uid: int,
        raw: bytes,
        raw_sha: str,
        parsed,
        reasons: list[dict[str, Any]],
        existing,
    ) -> None:
        account = self.settings.account
        mailbox = self.settings.imap_mailbox
        original_sha = self.store.put_original(raw)
        receipt = {
            "type": "rejection",
            "account": account,
            "mailbox": mailbox,
            "uidvalidity": uidvalidity,
            "uid": uid,
            "raw_sha256": raw_sha,
            "subject": parsed.subject if parsed else None,
            "from": parsed.from_addr if parsed else None,
            "date": parsed.date_header if parsed else None,
            "reasons": reasons,
            "created_at": utcnow(),
        }
        receipt_sha, receipt_rel = self.store.put_receipt(receipt)
        original_rel = f"msgs/{original_sha}.eml"
        primary = reasons[0]
        with self.db.connect() as conn:
            with transaction(conn):
                generation = _current_generation(conn, account, mailbox)
                if existing is None:
                    conn.execute(
                        """INSERT INTO message_record
                           (account, mailbox, uidvalidity, uid, generation,
                            raw_sha256, subject, from_addr, date_header,
                            decision, reason_code, reason, mark_state,
                            receipt_sha256, receipt_path, original_path,
                            report_nos, detail, scan_instance, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'rejected', ?, ?,
                                   'pending', ?, ?, ?, '[]', ?, ?, ?, ?)""",
                        (
                            account,
                            mailbox,
                            uidvalidity,
                            uid,
                            generation,
                            raw_sha,
                            parsed.subject if parsed else None,
                            parsed.from_addr if parsed else None,
                            parsed.date_header if parsed else None,
                            primary["code"],
                            primary["message"],
                            receipt_sha,
                            str(receipt_rel),
                            original_rel,
                            _json(reasons),
                            self.settings.instance_id,
                            utcnow(),
                            utcnow(),
                        ),
                    )
                else:
                    conn.execute(
                        """UPDATE message_record
                           SET generation=?, raw_sha256=?, subject=?, from_addr=?,
                               date_header=?, decision='rejected', reason_code=?,
                               reason=?, mark_state='pending', receipt_sha256=?,
                               receipt_path=?, original_path=?, report_nos='[]',
                               detail=?, scan_instance=?, updated_at=?
                           WHERE id=?""",
                        (
                            generation,
                            raw_sha,
                            parsed.subject if parsed else None,
                            parsed.from_addr if parsed else None,
                            parsed.date_header if parsed else None,
                            primary["code"],
                            primary["message"],
                            receipt_sha,
                            str(receipt_rel),
                            original_rel,
                            _json(reasons),
                            self.settings.instance_id,
                            utcnow(),
                            existing["id"],
                        ),
                    )
                    # A prior acceptance for this UID is being overturned; its
                    # traceability links must not survive, though the report
                    # rows it created stay in the archive.
                    conn.execute(
                        "DELETE FROM delivery WHERE message_id=?", (existing["id"],)
                    )

    def _commit_acceptance(
        self,
        uidvalidity: int,
        uid: int,
        raw: bytes,
        raw_sha: str,
        parsed,
        plans: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Persist an accepted email atomically.

        The latest committed original for each report number is read *inside*
        the write transaction.  If another instance stored a different original
        in the meantime, nothing from this email is inserted; ``conflict`` is
        returned so the caller can record the rejection.
        """
        original_sha = self.store.put_original(raw)
        original_rel = f"msgs/{original_sha}.eml"

        report_nos = [plan["report_no"] for plan in plans]
        try:
            return self._commit_acceptance_tx(
                uidvalidity, uid, raw, raw_sha, parsed, plans,
                original_rel, report_nos,
            )
        except _Conflict as exc:
            return {"conflict": exc.reasons}

    def _commit_acceptance_tx(
        self,
        uidvalidity: int,
        uid: int,
        raw: bytes,
        raw_sha: str,
        parsed,
        plans: list[dict[str, Any]],
        original_rel: str,
        report_nos: list[str],
    ) -> dict[str, Any]:
        account = self.settings.account
        mailbox = self.settings.imap_mailbox
        with self.db.connect() as conn:
            with transaction(conn):
                resolved: list[dict[str, Any]] = []
                conflicts: list[dict[str, Any]] = []
                for plan in plans:
                    row = conn.execute(
                        """SELECT id, attachment_sha256, attachment_path,
                                  attachment_name
                           FROM report
                           WHERE account=? AND mailbox=? AND report_no=?""",
                        (account, mailbox, plan["report_no"]),
                    ).fetchone()
                    if row is None:
                        resolved.append({**plan, "disposition": "new", "path": None})
                    elif row["attachment_sha256"] == plan["attachment_sha256"]:
                        resolved.append(
                            {
                                **plan,
                                "disposition": "reused",
                                "path": row["attachment_path"],
                                "name": row["attachment_name"],
                            }
                        )
                    else:
                        conflicts.append(
                            {
                                "code": "report_conflict",
                                "message": (
                                    f"报告号 {plan['report_no']} 已存在不同原件"
                                    f"（现有 {row['attachment_sha256'][:12]}…，"
                                    f"新件 {plan['attachment_sha256'][:12]}…）"
                                ),
                            }
                        )
                if conflicts:
                    # Abort without writing anything from this email.
                    raise _Conflict(conflicts)

                # Write files before rows so no committed row points at a
                # missing file.  Content addressing makes retries harmless.
                prepared: list[dict[str, Any]] = []
                reused_count = 0
                for item in resolved:
                    if item["disposition"] == "reused":
                        path = item["path"]
                        if not self.store.exists(path):
                            self.store.atomic_write_bytes(self.store.absolute(path), item["content"])
                        prepared.append(
                            {
                                "report_no": item["report_no"],
                                "sha256": item["attachment_sha256"],
                                "path": path,
                                "name": item["name"],
                                "rows": item["rows"],
                                "disposition": "reused",
                            }
                        )
                        reused_count += 1
                    else:
                        sha, rel = self.store.put_attachment(
                            item["report_no"], item["filename"], item["content"]
                        )
                        prepared.append(
                            {
                                "report_no": item["report_no"],
                                "sha256": sha,
                                "path": str(rel),
                                "name": item["filename"],
                                "rows": item["rows"],
                                "disposition": "new",
                            }
                        )

                receipt = {
                    "type": "acceptance",
                    "account": account,
                    "mailbox": mailbox,
                    "uidvalidity": uidvalidity,
                    "uid": uid,
                    "raw_sha256": raw_sha,
                    "original_path": original_rel,
                    "subject": parsed.subject,
                    "from": parsed.from_addr,
                    "date": parsed.date_header,
                    "reports": [
                        {
                            "report_no": item["report_no"],
                            "attachment_sha256": item["sha256"],
                            "attachment_name": item["name"],
                            "attachment_path": item["path"],
                            "disposition": item["disposition"],
                            "rows": item["rows"],
                        }
                        for item in prepared
                    ],
                    "created_at": utcnow(),
                }
                receipt_sha, receipt_rel = self.store.put_receipt(receipt)

                generation = _current_generation(conn, account, mailbox)
                cur = conn.execute(
                    """INSERT INTO message_record
                       (account, mailbox, uidvalidity, uid, generation,
                        raw_sha256, subject, from_addr, date_header,
                        decision, reason_code, reason, mark_state,
                        receipt_sha256, receipt_path, original_path,
                        report_nos, detail, scan_instance, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', NULL, NULL,
                               'pending', ?, ?, ?, ?, NULL, ?, ?, ?)""",
                    (
                        account,
                        mailbox,
                        uidvalidity,
                        uid,
                        generation,
                        raw_sha,
                        parsed.subject,
                        parsed.from_addr,
                        parsed.date_header,
                        receipt_sha,
                        str(receipt_rel),
                        original_rel,
                        _json(report_nos),
                        self.settings.instance_id,
                        utcnow(),
                        utcnow(),
                    ),
                )
                message_id = int(cur.lastrowid)
                for item in prepared:
                    conn.execute(
                        """INSERT OR IGNORE INTO report
                           (account, mailbox, report_no, sample_no, item, value,
                            rows_json, attachment_sha256, attachment_name,
                            attachment_path, source_message_id, created_at,
                            updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            account,
                            mailbox,
                            item["report_no"],
                            item["rows"][0][1],
                            item["rows"][0][2],
                            item["rows"][0][3],
                            _json(item["rows"]),
                            item["sha256"],
                            item["name"],
                            item["path"],
                            message_id,
                            utcnow(),
                            utcnow(),
                        ),
                    )
                    conn.execute(
                        """INSERT OR REPLACE INTO delivery
                           (message_id, report_no, attachment_sha256,
                            attachment_name, attachment_path, disposition, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            message_id,
                            item["report_no"],
                            item["sha256"],
                            item["name"],
                            item["path"],
                            item["disposition"],
                            utcnow(),
                        ),
                    )
        return {"reused": reused_count, "message_id": message_id}

    # ------------------------------------------------------------------ marks
    def _mark_pending(
        self,
        conn: imaplib.IMAP4,
        uidvalidity: int,
        token: str,
        result: ScanResult,
    ) -> None:
        """Store \\Seen for committed messages, then record that locally.

        Every mark is verified twice before it is issued:

        * the scan token must still own the current mailbox generation;
        * the message currently occupying the UID must have exactly the raw
          SHA-256 recorded for that UID in this generation.

        The second check is what makes reused UIDs safe.  If a mailbox was
        rebuilt while a mark was pending, UID N now belongs to a different
        message, the hash differs, and the row is quarantined as ``stale``
        instead of being marked with another message's receipt.
        """
        if not self.db.token_is_current(token, uidvalidity):
            return
        rows = self.db.pending_messages(
            self.settings.account, self.settings.imap_mailbox
        )
        for row in rows:
            if row["uidvalidity"] != uidvalidity:
                # Pending intent from an older mailbox generation.  Its UID
                # may already refer to a different message.
                self.db.set_mark_state(row["id"], "stale")
                continue
            if not self.db.token_is_current(token, uidvalidity):
                return
            try:
                typ, data = conn.uid("FETCH", str(row["uid"]), "(UID BODY.PEEK[])")
            except (imaplib.IMAP4.abort, OSError) as exc:
                self.db.increment_mark_attempts(row["id"])
                log.warning("mark precheck uid=%s failed (will retry): %s", row["uid"], exc)
                raise _LinkLost(str(exc)) from exc
            if typ != "OK":
                self.db.increment_mark_attempts(row["id"])
                log.warning("mark precheck uid=%s failed: %r", row["uid"], data)
                continue
            raw = self._extract_raw(data)
            if raw is None:
                # The UID no longer exists in this mailbox generation.
                self.db.set_mark_state(row["id"], "stale")
                continue
            if hashlib.sha256(raw).hexdigest() != row["raw_sha256"]:
                log.warning(
                    "uid=%s now holds different content; refusing to mark (stale)",
                    row["uid"],
                )
                self.db.set_mark_state(row["id"], "stale")
                continue
            try:
                typ, data = conn.uid("STORE", str(row["uid"]), "+FLAGS.SILENT", "(\\Seen)")
            except (imaplib.IMAP4.abort, OSError) as exc:
                # The server may have applied the flag before the connection
                # dropped; leave the row pending and reconcile on retry.
                self.db.increment_mark_attempts(row["id"])
                log.warning("mark uid=%s failed (will retry): %s", row["uid"], exc)
                raise _LinkLost(str(exc)) from exc
            if typ != "OK":
                self.db.increment_mark_attempts(row["id"])
                log.warning("mark uid=%s rejected by server: %r", row["uid"], data)
                continue
            # The flag may already have been stored by a previous attempt;
            # re-STORING +FLAGS is idempotent.
            self.db.set_mark_state(row["id"], "marked")
            result.counts["marked"] += 1

    def mark_pending_public(self) -> int:
        """Retry pending marks without ingesting new mail (web "retry" action)."""
        conn = self._connect()
        token = None
        try:
            typ, data = conn.select(self.settings.imap_mailbox)
            if typ != "OK":
                raise RuntimeError(f"SELECT failed: {data!r}")
            uidvalidity = self._uidvalidity(conn)
            claimed, token, _ = self.db.claim_scan(
                self.settings.account,
                self.settings.imap_mailbox,
                uidvalidity,
                self.settings.instance_id,
                self.settings.scan_lease_seconds,
            )
            if not claimed or token is None:
                raise ScanBusy("另一实例正在采集该邮箱或租约尚未过期")
            result = ScanResult(token=token, status="completed", uidvalidity=uidvalidity)
            try:
                self._mark_pending(conn, uidvalidity, token, result)
            except _LinkLost as exc:
                self.db.finish_scan(token, "interrupted", result.counts, str(exc))
                log.warning("retry-marks interrupted: %s", exc)
            else:
                self.db.finish_scan(token, "completed", result.counts)
            return result.counts["marked"]
        except Exception as exc:
            if token:
                try:
                    self.db.finish_scan(token, "failed", None, str(exc))
                except Exception:  # pragma: no cover
                    pass
            raise
        finally:
            try:
                conn.logout()
            except Exception:  # pragma: no cover
                pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _current_generation(conn, account: str, mailbox: str) -> int:
    row = conn.execute(
        "SELECT generation FROM mailbox_state WHERE account=? AND mailbox=?",
        (account, mailbox),
    ).fetchone()
    return int(row["generation"]) if row else 0
