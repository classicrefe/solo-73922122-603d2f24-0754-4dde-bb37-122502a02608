"""Durable storage for immutable message originals and receipts.

Files are content-addressed: originals are named by the SHA-256 of the raw
RFC822 message and stored under ``msgs/``; fixity receipts are named by the
SHA-256 of the receipt JSON and stored under ``receipts/``. Content addressing
makes persistence idempotent across retries and across the two collector
instances: writing the same bytes twice is a no-op.

The layout is intentionally plain so operators and the archive job can verify
it without the application:

    store/
      msgs/<sha256>.eml
      attachments/<report>/<sha256>-<safe-name>.csv
      receipts/<sha256>.json
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_component(value: str, fallback: str = "x") -> str:
    cleaned = _SAFE_RE.sub("_", value).strip("._")
    return cleaned[:80] or fallback


def canonical_json(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.msg_dir = self.root / "msgs"
        self.att_dir = self.root / "attachments"
        self.receipt_dir = self.root / "receipts"

    def init(self) -> None:
        for directory in (self.msg_dir, self.att_dir, self.receipt_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def atomic_write_bytes(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # Rename is atomic; if another process won the race the existing
            # content-addressed file is byte-identical, so replacing is safe.
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        # Durably record the directory entry.
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def put_original(self, raw: bytes) -> str:
        digest = sha256_bytes(raw)
        self.atomic_write_bytes(self.msg_dir / f"{digest}.eml", raw)
        return digest

    def put_attachment(self, report_no: str, filename: str, content: bytes) -> tuple[str, Path]:
        digest = sha256_bytes(content)
        rel = (
            Path("attachments")
            / safe_component(report_no, "unknown")
            / f"{digest}-{safe_component(filename, 'attachment.csv')}"
        )
        self.atomic_write_bytes(self.root / rel, content)
        return digest, rel

    def put_receipt(self, receipt: dict) -> tuple[str, Path]:
        payload = canonical_json(receipt)
        digest = sha256_bytes(payload)
        rel = Path("receipts") / f"{digest}.json"
        self.atomic_write_bytes(self.root / rel, payload + b"\n")
        return digest, rel

    def absolute(self, rel: Path | str) -> Path:
        return self.root / Path(rel)

    def read(self, rel: Path | str) -> bytes:
        return self.absolute(rel).read_bytes()

    def exists(self, rel: Path | str) -> bool:
        return self.absolute(rel).is_file()
