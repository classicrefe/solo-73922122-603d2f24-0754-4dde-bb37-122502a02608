"""RFC822 parsing and CSV validation.

An email is accepted only if *every* attachment is a well-formed UTF-8 CSV
within the size limits and each attachment contains exactly one report number.
Attachments are validated as one transaction: if any attachment fails, the
whole email is rejected and nothing from it is entered into the report store.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message
from email.policy import default as default_policy
from typing import Any

EXPECTED_HEADER = ["报告号", "样本号", "项目", "测值"]

REASON_MESSAGES = {
    "no_attachment": "邮件没有附件",
    "too_many_attachments": "附件数量超过上限",
    "attachment_too_large": "附件超过 256 KiB",
    "bad_extension": "附件不是 .csv 文件",
    "not_utf8": "附件不是合法的 UTF-8 文本",
    "bad_encoding": "附件未声明为 UTF-8 或含 UTF-8 BOM",
    "empty_attachment": "附件为空或没有数据行",
    "bad_header": "CSV 表头不是“报告号,样本号,项目,测值”",
    "bad_row_width": "CSV 行列数不为 4",
    "blank_field": "CSV 存在空字段",
    "multiple_reports": "同一附件包含多个报告号",
    "duplicate_report": "同一封邮件内重复出现同一报告号",
    "report_conflict": "同一报告号已存在不同原件",
    "bad_csv": "CSV 无法解析",
    "parse_error": "邮件无法解析",
}


def _reason(code: str, detail: str | None = None) -> dict[str, str]:
    message = REASON_MESSAGES.get(code, code)
    if detail:
        message = f"{message}：{detail}"
    return {"code": code, "message": message}


@dataclass
class Attachment:
    filename: str
    content: bytes
    content_type: str


@dataclass
class ParsedMail:
    subject: str
    from_addr: str
    date_header: str
    attachments: list[Attachment] = field(default_factory=list)


def parse_mail(raw: bytes) -> ParsedMail:
    msg: Message = message_from_bytes(raw, policy=default_policy)
    parsed = ParsedMail(
        subject=str(msg.get("Subject", "") or "").strip(),
        from_addr=str(msg.get("From", "") or "").strip(),
        date_header=str(msg.get("Date", "") or "").strip(),
    )
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        if disposition != "attachment":
            continue
        filename = part.get_filename() or ""
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = b""
        parsed.attachments.append(
            Attachment(
                filename=filename,
                content=payload,
                content_type=part.get_content_type(),
            )
        )
    return parsed


def _decode_utf8(content: bytes) -> tuple[str | None, dict[str, str] | None]:
    if content.startswith(b"\xef\xbb\xbf"):
        content = content[3:]
    try:
        return content.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return None, _reason("not_utf8", f"第 {exc.start} 字节无法按 UTF-8 解码")


def validate_attachment(
    attachment: Attachment,
    max_bytes: int,
) -> dict[str, Any]:
    """Validate one attachment and return a plan or a rejection reason."""
    name = attachment.filename or "(未命名)"
    if not name.lower().endswith(".csv"):
        return {"ok": False, "filename": name, "reason": _reason("bad_extension", name)}
    if len(attachment.content) > max_bytes:
        return {
            "ok": False,
            "filename": name,
            "reason": _reason("attachment_too_large", f"{len(attachment.content)} 字节"),
        }
    if len(attachment.content) == 0:
        return {"ok": False, "filename": name, "reason": _reason("empty_attachment", name)}
    declared_charset = None
    text, decode_error = _decode_utf8(attachment.content)
    del declared_charset
    if decode_error:
        return {"ok": False, "filename": name, "reason": decode_error}
    assert text is not None
    if not text.strip():
        return {"ok": False, "filename": name, "reason": _reason("empty_attachment", name)}
    if "\x00" in text:
        return {"ok": False, "filename": name, "reason": _reason("not_utf8", "包含 NUL 字节")}
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        return {"ok": False, "filename": name, "reason": _reason("bad_csv", str(exc))}
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return {"ok": False, "filename": name, "reason": _reason("empty_attachment", name)}
    header = [cell.lstrip("﻿").strip() for cell in rows[0]]
    if header != EXPECTED_HEADER:
        return {
            "ok": False,
            "filename": name,
            "reason": _reason("bad_header", ",".join(header)),
        }
    data_rows = rows[1:]
    if not data_rows:
        return {"ok": False, "filename": name, "reason": _reason("empty_attachment", name)}
    normalized: list[list[str]] = []
    report_no = None
    for lineno, row in enumerate(data_rows, start=2):
        if len(row) != 4:
            return {
                "ok": False,
                "filename": name,
                "reason": _reason("bad_row_width", f"第 {lineno} 行有 {len(row)} 列"),
            }
        cells = [cell.strip() for cell in row]
        if any(cell == "" for cell in cells):
            return {
                "ok": False,
                "filename": name,
                "reason": _reason("blank_field", f"第 {lineno} 行"),
            }
        if report_no is None:
            report_no = cells[0]
        elif cells[0] != report_no:
            return {
                "ok": False,
                "filename": name,
                "reason": _reason(
                    "multiple_reports", f"第 {lineno} 行为 {cells[0]!r}，首行为 {report_no!r}"
                ),
            }
        normalized.append(cells)
    assert report_no is not None
    return {
        "ok": True,
        "filename": name,
        "report_no": report_no,
        "rows": normalized,
        "sample_no": normalized[0][1],
        "item": normalized[0][2],
        "value": normalized[0][3],
        "content": attachment.content,
        "attachment_sha256": hashlib.sha256(attachment.content).hexdigest(),
    }


def validate_mail(
    parsed: ParsedMail,
    max_bytes: int,
    max_attachments: int,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, str]]]:
    """Validate a whole email atomically.

    Returns ``(plans, reasons)``.  Any reason means the email must be rejected
    in full; ``plans`` is then ``None``.
    """
    reasons: list[dict[str, str]] = []
    if not parsed.attachments:
        return None, [_reason("no_attachment")]
    if len(parsed.attachments) > max_attachments:
        return None, [
            _reason("too_many_attachments", f"{len(parsed.attachments)} 个，上限 {max_attachments}")
        ]
    plans: list[dict[str, Any]] = []
    seen: set[str] = set()
    for attachment in parsed.attachments:
        plan = validate_attachment(attachment, max_bytes)
        if not plan["ok"]:
            reasons.append(plan["reason"])
            continue
        if plan["report_no"] in seen:
            reasons.append(_reason("duplicate_report", plan["report_no"]))
            continue
        seen.add(plan["report_no"])
        plans.append(plan)
    if reasons:
        return None, reasons
    return plans, []
