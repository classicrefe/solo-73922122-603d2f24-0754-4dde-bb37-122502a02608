"""Cross-instance lease and progress semantics at the database layer."""

from __future__ import annotations

import time

from app.db import Database, transaction


def _db(tmp_path) -> Database:
    db = Database(tmp_path / "db.sqlite3")
    db.init()
    return db


def test_only_one_live_lease(tmp_path):
    db = _db(tmp_path)
    ok_a, token_a, _ = db.claim_scan("a@x", "INBOX", 1, "inst-a", 60)
    assert ok_a and token_a
    ok_b, token_b, _ = db.claim_scan("a@x", "INBOX", 1, "inst-b", 60)
    assert not ok_b and token_b is None
    assert db.token_is_current(token_a, 1)


def test_expired_lease_is_reclaimable(tmp_path):
    db = _db(tmp_path)
    ok, token, _ = db.claim_scan("a@x", "INBOX", 1, "inst-a", 0.01)
    assert ok
    time.sleep(0.05)
    ok_b, token_b, _ = db.claim_scan("a@x", "INBOX", 1, "inst-b", 60)
    assert ok_b and token_b and token_b != token
    assert not db.token_is_current(token, 1)


def test_old_token_cannot_advance_new_scan(tmp_path):
    db = _db(tmp_path)
    ok, old, _ = db.claim_scan("a@x", "INBOX", 1, "inst-a", 0.01)
    assert ok
    time.sleep(0.05)
    ok, new, _ = db.claim_scan("a@x", "INBOX", 1, "inst-b", 60)
    assert ok
    assert db.set_progress(new, 1, 5)
    assert not db.set_progress(old, 1, 99)
    state = db.get_state("a@x", "INBOX")
    assert state["last_uid"] == 5


def test_uidvalidity_change_clears_progress_and_lease(tmp_path):
    db = _db(tmp_path)
    ok, old, _ = db.claim_scan("a@x", "INBOX", 1, "inst-a", 60)
    assert ok
    db.set_progress(old, 1, 42)
    ok, new, _ = db.claim_scan("a@x", "INBOX", 2, "inst-b", 60)
    assert ok and new != old
    state = db.get_state("a@x", "INBOX")
    assert state["uidvalidity"] == 2
    assert state["last_uid"] == 0
    assert state["generation"] == 2
    # The old generation's token is now worthless.
    assert not db.token_is_current(old, 1)
    assert not db.set_progress(old, 1, 43)


def test_stale_scan_cannot_clear_new_lease(tmp_path):
    db = _db(tmp_path)
    ok, old, _ = db.claim_scan("a@x", "INBOX", 1, "inst-a", 0.01)
    assert ok
    time.sleep(0.05)
    ok, new, _ = db.claim_scan("a@x", "INBOX", 1, "inst-b", 60)
    assert ok
    db.finish_scan(old, "stale", {}, "too late")
    state = db.get_state("a@x", "INBOX")
    # The stale scan must not have released the new scan's lease.
    assert state["scan_token"] == new


def test_adopt_changed_uidvalidity_is_conditional(tmp_path):
    db = _db(tmp_path)
    ok, token, _ = db.claim_scan("a@x", "INBOX", 1, "inst-a", 60)
    assert ok
    assert db.adopt_changed_uidvalidity("a@x", "INBOX", 1, 2, token)
    state = db.get_state("a@x", "INBOX")
    assert state["uidvalidity"] == 2 and state["scan_token"] is None
    # A newer scan claimed UIDVALIDITY 3; the old token reporting 2 must not
    # roll progress back.
    ok, new_token, _ = db.claim_scan("a@x", "INBOX", 3, "inst-b", 60)
    assert ok
    assert not db.adopt_changed_uidvalidity("a@x", "INBOX", 1, 2, token)
    assert db.get_state("a@x", "INBOX")["uidvalidity"] == 3
    # The current owner can still report its own change.
    assert db.adopt_changed_uidvalidity("a@x", "INBOX", 3, 4, new_token)
    assert db.get_state("a@x", "INBOX")["uidvalidity"] == 4


def test_uid_anomaly_is_recorded_once(tmp_path):
    """The same (uid, observed content) anomaly is a single row; a different
    observed content for the same UID is a new row."""
    db = _db(tmp_path)
    # A committed message to hang the anomaly on.
    ok, token, _ = db.claim_scan("a@x", "INBOX", 1, "inst-a", 60)
    assert ok
    with db.connect() as conn:
        with transaction(conn):
            conn.execute(
                """INSERT INTO message_record
                   (account, mailbox, uidvalidity, uid, generation, raw_sha256,
                    decision, mark_state, receipt_sha256, receipt_path,
                    original_path, report_nos, created_at, updated_at)
                   VALUES ('a@x', 'INBOX', 1, 7, 1, 'old-sha', 'accepted',
                           'pending', 'r', 'receipts/r.json', 'msgs/o.eml',
                           '[]', 'now', 'now')"""
            )
    message_id = 1
    args = dict(
        account="a@x", mailbox="INBOX", uidvalidity=1, uid=7,
        message_id=message_id, expected_sha256="old-sha",
        reason_code="uid_content_changed", reason="swapped",
        scan_instance="inst-a",
    )
    assert db.record_uid_anomaly(observed_sha256="new-sha-1", **args) is True
    # Same observation again (also from the other instance): no new row.
    assert db.record_uid_anomaly(observed_sha256="new-sha-1", **{**args, "scan_instance": "inst-b"}) is False
    rows = db.list_anomalies()
    assert len(rows) == 1
    assert rows[0]["seen_count"] == 2
    assert rows[0]["message_id"] == message_id
    # A different swapped content for the same UID is a distinct anomaly.
    assert db.record_uid_anomaly(observed_sha256="new-sha-2", **args) is True
    assert len(db.list_anomalies()) == 2
    assert len(db.anomalies_for_message(message_id)) == 2
