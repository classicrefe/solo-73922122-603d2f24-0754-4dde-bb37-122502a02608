"""Web/API tests: UI actions, downloads, and two independent instances."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def _client(demo, instance: str = "inst-a") -> TestClient:
    collector = demo.make_collector(instance)
    app = create_app(collector.cfg)
    app.state.collector = collector
    app.state.db = collector.db
    app.state.store = collector.store
    return TestClient(app)


def test_dashboard_and_scan_action(demo):
    demo.deliver("web", [demo.attachment("R1"), demo.attachment("R2")])
    client = _client(demo)

    response = client.post("/actions/scan", follow_redirects=True)
    assert response.status_code == 200
    assert "收讫" in response.text
    assert "R1" in response.text

    status = client.get("/api/status").json()
    assert status["stats"]["accepted"] == 1
    assert status["stats"]["marked"] == 1
    assert status["imap"]["ok"] is True


def test_rejection_reason_visible_and_state_filter(demo):
    demo.deliver("bad", [{"filename": "R1.csv", "content": "bad,header\n1,2\n"}])
    client = _client(demo)
    client.post("/api/scan")

    page = client.get("/").text
    assert "拒收" in page
    assert "bad_header" in page or "CSV 表头" in page

    api = client.get("/api/messages", params={"mark_state": "marked"}).json()
    assert len(api) == 1 and api[0]["decision"] == "rejected"


def test_downloads(demo):
    demo.deliver("downloads", [demo.attachment("R1")])
    client = _client(demo)
    client.post("/api/scan")

    message = client.get("/api/messages").json()[0]
    original = client.get(f"/api/messages/{message['id']}/original")
    assert original.status_code == 200
    assert b"Subject: downloads" in original.content

    receipt = client.get(f"/api/messages/{message['id']}/receipt")
    assert receipt.status_code == 200
    assert json.loads(receipt.content)["type"] == "acceptance"

    report = client.get("/api/messages").json()[0]["reports"][0]
    attachment = client.get(f"/api/reports/{report['report_id']}/attachment")
    assert attachment.status_code == 200
    assert "报告号,样本号,项目,测值" in attachment.content.decode()


def test_retry_marks_endpoint(demo):
    demo.deliver("drop", [demo.attachment("R1")])
    demo.maildir.drop_next_store = True
    client = _client(demo)
    client.post("/api/scan")

    stats = client.get("/api/status").json()["stats"]
    assert stats["pending_marks"] == 1
    assert client.post("/api/retry-marks").json()["marked"] == 1
    stats = client.get("/api/status").json()["stats"]
    assert stats["pending_marks"] == 0 and stats["marked"] == 1


def test_second_instance_is_conflict_when_lease_taken(demo):
    demo.deliver("one", [demo.attachment("R1")])
    client_b = _client(demo, "inst-b")
    collector_a = demo.make_collector("inst-a")
    claimed, token, _ = client_b.app.state.db.claim_scan(
        "lab@test", "INBOX", 1, "inst-b", 60
    )
    assert claimed
    app_a = create_app(collector_a.cfg)
    app_a.state.collector = collector_a
    client_a = TestClient(app_a)
    response = client_a.post("/api/scan")
    assert response.status_code == 409
    assert "另一实例" in response.json()["detail"]


def test_missing_message_returns_404(demo):
    client = _client(demo)
    assert client.get("/api/messages/999").status_code == 404
