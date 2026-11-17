"""CSV/mail validation rules."""

from __future__ import annotations

from app.mailparse import validate_attachment, validate_mail, parse_mail
from imap_demo.server import build_demo_mail


def _attachment(filename: str, content: bytes):
    from app.mailparse import Attachment

    return Attachment(filename=filename, content=content, content_type="text/csv")


def test_valid_csv_plan():
    plan = validate_attachment(
        _attachment("R1.csv", "报告号,样本号,项目,测值\nR1,S1,ALT,10\nR1,S1,GLU,5\n".encode()),
        256 * 1024,
    )
    assert plan["ok"]
    assert plan["report_no"] == "R1"
    assert len(plan["rows"]) == 2
    assert plan["attachment_sha256"]


def test_bom_is_accepted():
    plan = validate_attachment(
        _attachment("R1.csv", b"\xef\xbb\xbf" + "报告号,样本号,项目,测值\nR1,S1,ALT,10\n".encode()),
        256 * 1024,
    )
    assert plan["ok"]


def test_not_utf8_rejected():
    plan = validate_attachment(_attachment("R1.csv", b"\xff\xfe\x00a"), 256 * 1024)
    assert not plan["ok"]
    assert plan["reason"]["code"] == "not_utf8"


def test_wrong_extension_rejected():
    plan = validate_attachment(
        _attachment("R1.txt", "报告号,样本号,项目,测值\nR1,S1,ALT,10\n".encode()), 256 * 1024
    )
    assert plan["reason"]["code"] == "bad_extension"


def test_oversize_rejected():
    plan = validate_attachment(
        _attachment("R1.csv", "报告号,样本号,项目,测值\nR1,S1,ALT,".encode() + b"9" * 1024),
        1024,
    )
    assert plan["reason"]["code"] == "attachment_too_large"


def test_multiple_reports_in_one_attachment_rejected():
    plan = validate_attachment(
        _attachment("R.csv", "报告号,样本号,项目,测值\nR1,S1,ALT,1\nR2,S2,ALT,2\n".encode()),
        256 * 1024,
    )
    assert plan["reason"]["code"] == "multiple_reports"


def test_blank_field_rejected():
    plan = validate_attachment(
        _attachment("R.csv", "报告号,样本号,项目,测值\nR1,,ALT,1\n".encode()), 256 * 1024
    )
    assert plan["reason"]["code"] == "blank_field"


def test_wrong_header_rejected():
    plan = validate_attachment(
        _attachment("R.csv", "report,sample,item,value\nR1,S1,ALT,1\n".encode()), 256 * 1024
    )
    assert plan["reason"]["code"] == "bad_header"


def test_two_and_three_attachments_accepted():
    raw = build_demo_mail(
        {
            "attachments": [
                {"filename": "A.csv", "report_no": "A"},
                {"filename": "B.csv", "report_no": "B"},
                {"filename": "C.csv", "report_no": "C"},
            ]
        }
    )
    plans, reasons = validate_mail(parse_mail(raw), 256 * 1024, 3)
    assert reasons == []
    assert [p["report_no"] for p in plans] == ["A", "B", "C"]


def test_four_attachments_rejected_even_if_valid():
    raw = build_demo_mail(
        {
            "attachments": [
                {"filename": f"{n}.csv", "report_no": n} for n in ("A", "B", "C", "D")
            ]
        }
    )
    plans, reasons = validate_mail(parse_mail(raw), 256 * 1024, 3)
    assert plans is None
    assert reasons[0]["code"] == "too_many_attachments"


def test_one_bad_attachment_rejects_whole_mail():
    raw = build_demo_mail(
        {
            "attachments": [
                {"filename": "A.csv", "report_no": "A"},
                {"filename": "B.csv", "content": "not,a,valid,header\n1,2,3,4\n"},
            ]
        }
    )
    plans, reasons = validate_mail(parse_mail(raw), 256 * 1024, 3)
    assert plans is None
    assert reasons[0]["code"] == "bad_header"


def test_duplicate_report_in_same_mail_rejected():
    raw = build_demo_mail(
        {
            "attachments": [
                {"filename": "A.csv", "report_no": "A"},
                {"filename": "A-again.csv", "report_no": "A"},
            ]
        }
    )
    plans, reasons = validate_mail(parse_mail(raw), 256 * 1024, 3)
    assert plans is None
    assert reasons[0]["code"] == "duplicate_report"


def test_no_attachment_rejected():
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = "no attachment"
    msg["From"] = "lab@example.test"
    msg.set_content("body only\n")
    plans, reasons = validate_mail(parse_mail(msg.as_bytes()), 256 * 1024, 3)
    assert plans is None
    assert reasons[0]["code"] == "no_attachment"
