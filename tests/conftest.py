"""Shared test harness: an in-process demo IMAP server and a temporary app."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from app.collector import Collector  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.store import Store  # noqa: E402
from imap_demo.server import DemoServer, MailboxStore, build_demo_mail  # noqa: E402


@pytest.fixture()
def demo(tmp_path):
    store = MailboxStore(tmp_path / "maildir-data")
    server = DemoServer(("127.0.0.1", 0), store)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Demo(store, port, tmp_path)
    finally:
        server.shutdown()
        server.server_close()


class Demo:
    def __init__(self, maildir: MailboxStore, port: int, tmp_path: Path):
        self.maildir = maildir
        self.port = port
        self.tmp_path = tmp_path
        self._counter = 0

    # ---------------------------------------------------------------- helpers
    def make_collector(self, instance: str = "inst-a") -> Collector:
        cfg = Settings(
            instance_id=instance,
            database_path=self.tmp_path / "db.sqlite3",
            storage_dir=self.tmp_path / "store",
            imap_host="127.0.0.1",
            imap_port=self.port,
            imap_user="lab",
            imap_password="lab-pass",
            account="lab@test",
            auto_scan_interval=0,
            scan_lease_seconds=60,
        )
        db = Database(cfg.database_path)
        db.init()
        store = Store(cfg.storage_dir)
        store.init()
        collector = Collector(cfg, db, store)
        collector.cfg = cfg  # type: ignore[attr-defined]
        return collector

    def deliver(self, subject: str = "mail", attachments=None, **kwargs) -> int:
        payload = {"subject": subject}
        if attachments is not None:
            payload["attachments"] = attachments
        payload.update(kwargs)
        return self.maildir.deliver(build_demo_mail(payload))

    @staticmethod
    def attachment(report_no: str, value: str = "12", rows=None) -> dict:
        return {
            "filename": f"{report_no}.csv",
            "report_no": report_no,
            "sample_no": "S001",
            "item": "ALT",
            "value": value,
            "rows": rows,
        }

    @staticmethod
    def csv_attachment(filename: str, content: str) -> dict:
        return {"filename": filename, "content": content}
