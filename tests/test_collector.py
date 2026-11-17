"""Collector behaviour against the demo IMAP server."""

from __future__ import annotations

import json

import pytest

from app.collector import ScanBusy, ScanStale


def _messages(collector):
    return collector.db.list_messages(100)


def _store_files(collector):
    root = collector.store.root
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_accepts_all_attachments_and_marks_after_commit(demo):
    demo.deliver("two attachments", [demo.attachment("R1"), demo.attachment("R2")])
    collector = demo.make_collector()
    result = collector.scan()

    assert result.counts["accepted"] == 1
    assert result.counts["marked"] == 1
    messages = _messages(collector)
    assert len(messages) == 1
    row = messages[0]
    assert row["decision"] == "accepted"
    assert row["mark_state"] == "marked"
    assert json.loads(row["report_nos"]) == ["R1", "R2"]

    # Both attachments plus original plus receipt are on disk.
    files = _store_files(collector)
    assert len([f for f in files if f.endswith(".csv")]) == 2
    assert any(f.startswith("msgs/") for f in files)
    assert any(f.startswith("receipts/") for f in files)
    assert all(m["flags"] == ["\\Seen"] for m in demo.maildir.all_flags())

    reports = collector.db.list_reports()
    assert {r["report_no"] for r in reports} == {"R1", "R2"}


def test_partially_invalid_mail_is_fully_rejected(demo):
    demo.deliver(
        "mixed",
        [
            demo.attachment("R1"),
            {"filename": "broken.csv", "content": "a,b\n1,2\n"},
        ],
    )
    collector = demo.make_collector()
    result = collector.scan()

    assert result.counts["rejected"] == 1
    assert collector.db.list_reports() == []
    message = _messages(collector)[0]
    assert message["decision"] == "rejected"
    assert len(json.loads(message["detail"])) == 1
    # Rejected raw messages are still persisted and acknowledged.
    assert message["mark_state"] == "marked"


def test_reuse_of_same_original_is_not_a_duplicate_report(demo):
    demo.deliver("first", [demo.attachment("R1")])
    collector = demo.make_collector()
    collector.scan()
    demo.deliver("second", [demo.attachment("R1")])
    second = collector.scan()

    assert second.counts["reused_reports"] == 1
    assert len(collector.db.list_reports()) == 1
    accepted = [m for m in _messages(collector) if m["decision"] == "accepted"]
    assert len(accepted) == 2


def test_same_report_number_different_original_rejects_whole_mail(demo):
    demo.deliver("first", [demo.attachment("R1")])
    collector = demo.make_collector()
    collector.scan()
    demo.deliver(
        "conflict",
        [
            demo.attachment("R2"),
            {"filename": "R1.csv", "content": "报告号,样本号,项目,测值\nR1,S9,XXX,99\n"},
        ],
    )
    result = collector.scan()

    assert result.counts["rejected"] == 1
    report_nos = {r["report_no"] for r in collector.db.list_reports()}
    # R2 must not leak in from the rejected mail.
    assert report_nos == {"R1"}
    last = _messages(collector)[0]
    assert last["decision"] == "rejected"
    assert json.loads(last["detail"])[0]["code"] == "report_conflict"


def test_second_scan_does_not_duplicate_reports(demo):
    demo.deliver("one", [demo.attachment("R1"), demo.attachment("R2")])
    collector = demo.make_collector()
    collector.scan()
    again = collector.scan()

    assert again.counts["accepted"] == 0
    assert again.counts["reused_reports"] == 0
    assert len(collector.db.list_reports()) == 2
    assert len(_messages(collector)) == 1


def test_pre_read_message_is_collected(demo):
    """A human opening the mail first must not make the attachment invisible."""
    demo.deliver("pre-read", [demo.attachment("R1")])
    demo.maildir.set_flag(1, "\\Seen", True)
    collector = demo.make_collector()
    result = collector.scan()

    assert result.counts["accepted"] == 1
    assert collector.db.list_reports()[0]["report_no"] == "R1"


def test_drop_after_mark_is_not_reported_twice(demo):
    """STORE succeeds on the server but the reply is lost."""
    demo.deliver("drop", [demo.attachment("R1")])
    demo.maildir.drop_next_store = True
    collector = demo.make_collector()
    result = collector.scan()

    assert result.counts["accepted"] == 1
    assert result.counts["marked"] == 0
    row = _messages(collector)[0]
    assert row["mark_state"] == "pending"
    # The server did apply \Seen even though the client saw a disconnect.
    assert demo.maildir.all_flags()[0]["flags"] == ["\\Seen"]

    marked = collector.mark_pending_public()
    assert marked == 1
    assert _messages(collector)[0]["mark_state"] == "marked"
    assert len(collector.db.list_reports()) == 1

    # A later full scan sees the same message and must not add reports.
    collector.scan()
    assert len(collector.db.list_reports()) == 1
    assert len(_messages(collector)) == 1


def test_uidvalidity_change_quarantines_pending_mark_and_reused_uid(demo):
    demo.deliver("old", [demo.attachment("R1")])
    demo.maildir.drop_next_store = True
    collector = demo.make_collector()
    collector.scan()
    assert _messages(collector)[0]["mark_state"] == "pending"

    # Rebuild with the same messages; UIDs restart at 1 and a brand-new
    # message also takes UID 1 afterwards.
    demo.maildir.rebuild(purge=True)
    demo.deliver("brand new uid 1", [demo.attachment("R2")])
    result = collector.scan()

    assert result.uidvalidity == 2
    rows = _messages(collector)
    old = [r for r in rows if r["uidvalidity"] == 1][0]
    assert old["mark_state"] == "stale"
    # The old receipt/UID was never applied to the new message.
    new = [r for r in rows if r["uidvalidity"] == 2][0]
    assert new["report_nos"] == '["R2"]'
    assert new["mark_state"] == "marked"
    assert new["raw_sha256"] != old["raw_sha256"]
    assert json.loads(new["report_nos"]) == ["R2"]


def test_uidvalidity_change_mid_scan_stops_progress_and_marks(demo, monkeypatch):
    for index in range(1, 6):
        demo.deliver(f"m{index}", [demo.attachment(f"R{index}")])
    collector = demo.make_collector()
    original = collector._process_message
    calls = {"n": 0}

    def sabotage(*args, **kwargs):
        outcome = original(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 2:
            demo.maildir.rebuild(purge=False)
            demo.deliver("post-rebuild", [demo.attachment("R99")])
        return outcome

    monkeypatch.setattr(collector, "_process_message", sabotage)
    with pytest.raises(ScanStale):
        collector.scan()

    state = collector.db.get_state("lab@test", "INBOX")
    assert state["uidvalidity"] == 2
    assert state["last_uid"] == 0
    assert state["scan_token"] is None
    flags = demo.maildir.all_flags()
    # The stale scan did not mark anything: everything stays unseen.
    assert all(entry["flags"] == [] for entry in flags)
    assert all(row["mark_state"] == "stale" for row in _messages(collector))


def test_concurrent_scan_is_refused(demo):
    demo.deliver("one", [demo.attachment("R1")])
    collector_a = demo.make_collector("inst-a")
    collector_b = demo.make_collector("inst-b")
    # Instance B owns a live lease.
    claimed, token, _ = collector_b.db.claim_scan("lab@test", "INBOX", 1, "inst-b", 60)
    assert claimed and token

    with pytest.raises(ScanBusy):
        collector_a.scan()
    assert collector_a.db.list_reports() == []
    assert demo.maildir.all_flags()[0]["flags"] == []


def test_progress_is_per_uidvalidity_and_survives_restart(demo):
    demo.deliver("one", [demo.attachment("R1")])
    collector = demo.make_collector()
    collector.scan()
    # A fresh collector object (process restart) shares the same database.
    restarted = demo.make_collector()
    result = restarted.scan()
    assert result.counts["accepted"] == 0
    assert len(restarted.db.list_reports()) == 1


def test_original_and_receipt_downloadable(demo):
    demo.deliver("download", [demo.attachment("R1")])
    collector = demo.make_collector()
    collector.scan()
    row = _messages(collector)[0]
    original = collector.store.read(row["original_path"])
    receipt = json.loads(collector.store.read(row["receipt_path"]))
    assert b"Subject: download" in original
    assert receipt["type"] == "acceptance"
    assert receipt["raw_sha256"] == row["raw_sha256"]
    assert receipt["reports"][0]["report_no"] == "R1"
