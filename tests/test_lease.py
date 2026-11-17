"""Cross-instance lease and progress semantics at the database layer."""

from __future__ import annotations

import time

from app.db import Database


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
