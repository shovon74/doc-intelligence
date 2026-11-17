import shutil
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import fitz
import pandas as pd
import pytest
from PIL import Image

from app.processing import pdf_processor, spreadsheet_processor
from app.processing.errors import DocumentError, ErrorCode, get_error_info
from app.processing.pdf_processor import stream_pdf_pages
from app.processing.pipeline import process_document
from app.storage import file_handler
from app.storage.file_handler import _resolve_mime

LONG_TEXT = "Invoice INV-001 total 120.00 EUR supplier ACME Ltd " * 3


def make_pdf(path: Path, page_texts: list[str]) -> Path:
    doc = fitz.open()
    for t in page_texts:
        page = doc.new_page()
        if t:
            page.insert_textbox(fitz.Rect(40, 40, 550, 800), t, fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


class FakeOCR:
    def __init__(self, text="scanned text from ocr"):
        self.calls = 0
        self.text = text

    def ocr_from_bytes(self, b):
        self.calls += 1
        assert b[:4] == b"\x89PNG"
        return self.text, 0.9


# ---------------------------------------------------------------- PDF
@pytest.mark.unit
def test_pdf_digital_pages_do_not_trigger_ocr(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", [LONG_TEXT, LONG_TEXT])
    ocr = FakeOCR()
    pages = list(stream_pdf_pages(pdf, ocr_processor=ocr))
    assert [p.page_number for p in pages] == [1, 2]
    assert all(p.source_type == "digital" and "INV-001" in p.text for p in pages)
    assert ocr.calls == 0


@pytest.mark.unit
def test_pdf_blank_page_falls_back_to_ocr(tmp_path):
    pdf = make_pdf(tmp_path / "b.pdf", [LONG_TEXT, ""])
    ocr = FakeOCR()
    pages = list(stream_pdf_pages(pdf, ocr_processor=ocr))
    assert pages[0].source_type == "digital"
    assert pages[1].source_type == "ocr"
    assert pages[1].text == "scanned text from ocr"
    assert pages[1].ocr_confidence == 0.9
    assert ocr.calls == 1


@pytest.mark.unit
def test_pdf_ocr_failure_is_isolated_to_page(tmp_path):
    pdf = make_pdf(tmp_path / "c.pdf", ["", LONG_TEXT])
    ocr = MagicMock()
    ocr.ocr_from_bytes.side_effect = RuntimeError("tesseract crashed")
    pages = list(stream_pdf_pages(pdf, ocr_processor=ocr))
    assert pages[0].error == "tesseract crashed"
    assert pages[1].error is None and pages[1].text


@pytest.mark.unit
def test_pdf_without_ocr_returns_digital_text(tmp_path):
    pdf = make_pdf(tmp_path / "d.pdf", [""])
    (page,) = stream_pdf_pages(pdf, ocr_processor=None)
    assert page.source_type == "digital" and page.text == ""


@pytest.mark.unit
def test_pdf_corrupt_file_raises(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    with pytest.raises(DocumentError) as ei:
        list(stream_pdf_pages(bad))
    assert ei.value.code is ErrorCode.CORRUPT_FILE
    with pytest.raises(DocumentError):
        pdf_processor.get_page_count(bad)


@pytest.mark.unit
def test_pdf_page_count(tmp_path):
    assert pdf_processor.get_page_count(make_pdf(tmp_path / "e.pdf", ["a", "b", "c"])) == 3


# ---------------------------------------------------------------- spreadsheets
@pytest.mark.unit
def test_csv_semicolon_delimiter_and_leading_zeros(tmp_path):
    f = tmp_path / "inv.csv"
    f.write_text("Invoice No;Total\n00123;10,5\n00124;20,0\n", encoding="utf-8")
    (sheet,) = spreadsheet_processor.stream_sheets(f, "csv")
    assert sheet.columns == ["Invoice No", "Total"]
    assert sheet.rows[0]["Invoice No"] == "00123"
    assert sheet.row_count == 2 and "00124" in sheet.text


@pytest.mark.unit
def test_xlsx_multi_sheet_and_cleanup(tmp_path):
    f = tmp_path / "multi.xlsx"
    with pd.ExcelWriter(f) as w:
        pd.DataFrame({"Item": ["a", "b", None], "Qty": [1, 2, None]}).to_excel(
            w, sheet_name="Items", index=False
        )
        pd.DataFrame({"Item": ["x"], "Item ": ["y"]}).to_excel(w, sheet_name="Dups", index=False)
        pd.DataFrame().to_excel(w, sheet_name="Empty", index=False)
    sheets = list(spreadsheet_processor.stream_sheets(f, "xlsx"))
    assert [s.sheet_name for s in sheets] == ["Items", "Dups", "Empty"]
    assert [s.page_number for s in sheets] == [1, 2, 3]
    assert sheets[0].row_count == 2                       # all-NaN row dropped
    assert sheets[0].rows[0] == {"Item": "a", "Qty": 1}   # numpy scalars -> python
    assert sheets[1].columns == ["Item", "Item_2"]        # duplicate headers de-duped
    assert sheets[2].row_count == 0 and not sheets[2].error
    assert spreadsheet_processor.count_sheets(f, "xlsx") == 3


@pytest.mark.unit
def test_corrupt_workbook_raises(tmp_path):
    f = tmp_path / "bad.xlsx"
    f.write_bytes(b"garbage")
    with pytest.raises(DocumentError) as ei:
        list(spreadsheet_processor.stream_sheets(f, "xlsx"))
    assert ei.value.code is ErrorCode.CORRUPT_FILE


# ---------------------------------------------------------------- file handler
@pytest.mark.unit
@pytest.mark.parametrize("detected,name,expected", [
    ("application/pdf", "x.pdf", "application/pdf"),
    ("application/zip", "x.xlsx",
     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("application/CDFV2", "x.xls", "application/vnd.ms-excel"),
    ("application/zip", "x.docx", "application/zip"),      # not allowed, stays rejected
    ("application/zip", "x.csv", "application/zip"),       # extension can't launder a zip
    ("image/x-tiff", "scan.TIF", "image/tiff"),
])
def test_resolve_mime(detected, name, expected):
    assert _resolve_mime(detected, name) == expected


@pytest.mark.unit
def test_save_upload_rejects_oversize_and_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(file_handler, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(file_handler, "MAX_BYTES", 10)
    with pytest.raises(DocumentError) as ei:
        file_handler.save_upload(b"x" * 11, "a.pdf", MagicMock())
    assert ei.value.code is ErrorCode.FILE_TOO_LARGE
    monkeypatch.setattr(file_handler, "MAX_BYTES", 10**6)
    monkeypatch.setattr(file_handler, "_detect_mime", lambda p: "application/x-msdownload")
    with pytest.raises(DocumentError) as ei:
        file_handler.save_upload(b"MZ....", "a.pdf", MagicMock())
    assert ei.value.code is ErrorCode.UNSUPPORTED_TYPE
    assert list(tmp_path.iterdir()) == []                  # temp dirs cleaned up


@pytest.mark.unit
def test_save_upload_stores_and_detects_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(file_handler, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(file_handler, "_detect_mime", lambda p: "application/pdf")
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    saved = file_handler.save_upload(b"%PDF-1.4 data", "inv.pdf", db)
    assert not saved["is_duplicate"]
    assert Path(saved["stored_path"]).read_bytes() == b"%PDF-1.4 data"
    assert saved["stored_path"].endswith(f"{saved['document_id']}.pdf")

    existing = MagicMock(id=uuid.uuid4(), stored_path="/x.pdf",
                         mime_type="application/pdf", file_ext="pdf")
    db.query.return_value.filter.return_value.first.return_value = existing
    dup = file_handler.save_upload(b"%PDF-1.4 data", "again.pdf", db)
    assert dup["is_duplicate"] and dup["document_id"] == str(existing.id)
    assert [p.name for p in tmp_path.iterdir()] == [Path(saved["stored_path"]).parent.name]


# ---------------------------------------------------------------- pipeline
def _fake_doc(path, ext):
    return MagicMock(id=uuid.uuid4(), stored_path=str(path), file_ext=ext,
                     original_filename=Path(path).name)


def _db_for(doc):
    db = MagicMock()
    db.get.return_value = doc
    return db


@pytest.mark.unit
def test_pipeline_pdf_persists_pages_and_sets_status(tmp_path):
    pdf = make_pdf(tmp_path / "p.pdf", [LONG_TEXT] * 45)
    doc = _fake_doc(pdf, "pdf")
    db = _db_for(doc)
    progress = []
    result = process_document(db, doc.id, on_progress=progress.append)
    assert result["pages"] == 45 and result["failed_pages"] == 0
    assert doc.page_count == 45 and doc.status == "extracting"
    assert progress[-1] == 50.0 and progress == sorted(progress)
    pages = [p for c in db.add_all.call_args_list for p in c.args[0]]
    assert [p.page_number for p in pages] == list(range(1, 46))
    assert db.add_all.call_count == 3                      # flushed in batches of 20, 20, 5


@pytest.mark.unit
def test_pipeline_csv(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("a,b\n1,2\n")
    doc = _fake_doc(f, "csv")
    result = process_document(_db_for(doc), doc.id)
    assert result["pages"] == 1 and doc.status == "extracting"


def _assert_friendly_failure(doc, code, secret=None):
    """Document is failed, carries the standard user-facing message and no internals."""
    assert doc.status == "failed" and doc.error_code == code.value
    assert doc.error_message == get_error_info(code).message
    if secret:
        assert secret not in doc.error_message


def _with_ocr(monkeypatch, ocr):
    monkeypatch.setattr("app.processing.pipeline.image_processor", ocr)


@pytest.mark.unit
def test_pipeline_missing_file_marks_failed(tmp_path):
    doc = _fake_doc(tmp_path / "gone.pdf", "pdf")
    with pytest.raises(DocumentError) as ei:
        process_document(_db_for(doc), doc.id)
    assert ei.value.code is ErrorCode.INTERNAL_ERROR
    _assert_friendly_failure(doc, ErrorCode.INTERNAL_ERROR, secret="gone.pdf")


@pytest.mark.unit
def test_pipeline_corrupt_pdf_marks_failed(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"junk")
    doc = _fake_doc(bad, "pdf")
    with pytest.raises(DocumentError) as ei:
        process_document(_db_for(doc), doc.id)
    assert ei.value.code is ErrorCode.CORRUPT_FILE
    _assert_friendly_failure(doc, ErrorCode.CORRUPT_FILE, secret="Cannot open")


@pytest.mark.unit
def test_pipeline_encrypted_pdf_marks_failed(tmp_path):
    src = make_pdf(tmp_path / "plain.pdf", [LONG_TEXT])
    enc = tmp_path / "enc.pdf"
    d = fitz.open(str(src))
    d.save(str(enc), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    d.close()
    doc = _fake_doc(enc, "pdf")
    with pytest.raises(DocumentError) as ei:
        process_document(_db_for(doc), doc.id)
    assert ei.value.code is ErrorCode.ENCRYPTED_PDF
    _assert_friendly_failure(doc, ErrorCode.ENCRYPTED_PDF)


@pytest.mark.unit
def test_pipeline_unexpected_exception_becomes_friendly_internal_error(tmp_path, monkeypatch):
    f = tmp_path / "a.csv"
    f.write_text("a,b\n1,2\n")
    doc = _fake_doc(f, "csv")
    db = _db_for(doc)

    def boom(*a, **k):
        raise KeyError("/srv/secret/internal/path")

    monkeypatch.setattr("app.processing.pipeline._plan", boom)
    with pytest.raises(DocumentError) as ei:
        process_document(db, doc.id)
    assert ei.value.code is ErrorCode.INTERNAL_ERROR
    _assert_friendly_failure(doc, ErrorCode.INTERNAL_ERROR, secret="/srv/secret")
    # technical detail is kept for operators in the event details only
    event = db.add.call_args_list[-1].args[0]
    assert event.status == "failed" and "/srv/secret" in event.details["detail"]


@pytest.mark.unit
def test_pipeline_all_pages_failed_uses_ocr_failed(tmp_path, monkeypatch):
    doc = _fake_doc(make_pdf(tmp_path / "scan.pdf", ["", ""]), "pdf")
    ocr = MagicMock()
    ocr.ocr_from_bytes.side_effect = RuntimeError("tesseract exploded at /usr/bin")
    _with_ocr(monkeypatch, ocr)
    with pytest.raises(DocumentError) as ei:
        process_document(_db_for(doc), doc.id)
    assert ei.value.code is ErrorCode.OCR_FAILED
    _assert_friendly_failure(doc, ErrorCode.OCR_FAILED, secret="tesseract exploded")


@pytest.mark.unit
def test_failed_page_stores_friendly_message_not_raw_error(tmp_path, monkeypatch):
    doc = _fake_doc(make_pdf(tmp_path / "mix.pdf", [LONG_TEXT, ""]), "pdf")
    db = _db_for(doc)
    ocr = MagicMock()
    ocr.ocr_from_bytes.side_effect = RuntimeError("raw tesseract failure")
    _with_ocr(monkeypatch, ocr)
    result = process_document(db, doc.id)
    assert result["failed_pages"] == 1 and doc.status == "extracting"
    bad = [p for c in db.add_all.call_args_list for p in c.args[0] if p.status == "failed"]
    assert bad[0].error_message == get_error_info(ErrorCode.CORRUPT_FILE).message
    assert "raw tesseract" not in bad[0].error_message


@pytest.mark.unit
def test_pipeline_unknown_document():
    db = MagicMock()
    db.get.return_value = None
    with pytest.raises(DocumentError) as ei:
        process_document(db, uuid.uuid4())
    assert ei.value.code is ErrorCode.INTERNAL_ERROR


# ---------------------------------------------------------------- empty / no-text (2.3)
@pytest.mark.unit
def test_save_upload_rejects_zero_byte_file(tmp_path, monkeypatch):
    monkeypatch.setattr(file_handler, "UPLOAD_DIR", tmp_path)
    with pytest.raises(DocumentError) as ei:
        file_handler.save_upload(b"", "empty.pdf", MagicMock())
    assert ei.value.code is ErrorCode.EMPTY_FILE
    assert list(tmp_path.iterdir()) == []          # nothing written to disk


@pytest.mark.unit
def test_pipeline_zero_byte_stored_file_marks_failed(tmp_path):
    f = tmp_path / "empty.pdf"
    f.write_bytes(b"")
    doc = _fake_doc(f, "pdf")
    with pytest.raises(DocumentError) as ei:
        process_document(_db_for(doc), doc.id)
    assert ei.value.code is ErrorCode.EMPTY_FILE
    _assert_friendly_failure(doc, ErrorCode.EMPTY_FILE, secret="empty.pdf")


@pytest.mark.unit
def test_csv_with_only_whitespace_is_empty_file(tmp_path):
    f = tmp_path / "blank.csv"
    f.write_text("\n\n  \n")
    with pytest.raises(DocumentError) as ei:
        list(spreadsheet_processor.stream_sheets(f, "csv"))
    assert ei.value.code is ErrorCode.EMPTY_FILE


@pytest.mark.unit
def test_pipeline_blank_pdf_without_any_text_is_no_text_found(tmp_path, monkeypatch):
    doc = _fake_doc(make_pdf(tmp_path / "blank.pdf", ["", ""]), "pdf")
    db = _db_for(doc)
    _with_ocr(monkeypatch, FakeOCR(text=""))           # OCR finds nothing either
    with pytest.raises(DocumentError) as ei:
        process_document(db, doc.id)
    assert ei.value.code is ErrorCode.NO_TEXT_FOUND
    _assert_friendly_failure(doc, ErrorCode.NO_TEXT_FOUND)
    assert db.add_all.called                            # pages kept for inspection


@pytest.mark.unit
def test_pipeline_header_only_csv_is_no_text_found(tmp_path):
    f = tmp_path / "headers.csv"
    f.write_text("invoice,total\n")
    doc = _fake_doc(f, "csv")
    with pytest.raises(DocumentError) as ei:
        process_document(_db_for(doc), doc.id)
    assert ei.value.code is ErrorCode.NO_TEXT_FOUND


@pytest.mark.unit
def test_pipeline_workbook_with_only_empty_sheets_is_no_text_found(tmp_path):
    f = tmp_path / "empty.xlsx"
    with pd.ExcelWriter(f) as w:
        pd.DataFrame().to_excel(w, sheet_name="One", index=False)
        pd.DataFrame().to_excel(w, sheet_name="Two", index=False)
    doc = _fake_doc(f, "xlsx")
    with pytest.raises(DocumentError) as ei:
        process_document(_db_for(doc), doc.id)
    assert ei.value.code is ErrorCode.NO_TEXT_FOUND


@pytest.mark.unit
def test_one_blank_page_among_text_pages_still_succeeds(tmp_path, monkeypatch):
    doc = _fake_doc(make_pdf(tmp_path / "ok.pdf", [LONG_TEXT, ""]), "pdf")
    _with_ocr(monkeypatch, FakeOCR(text=""))
    result = process_document(_db_for(doc), doc.id)
    assert result["pages"] == 2 and doc.status == "extracting"


# ---------------------------------------------------------------- OCR (needs tesseract)
@pytest.mark.unit
@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_image_ocr_and_multipage_tiff(tmp_path):
    from PIL import ImageDraw, ImageFont
    from app.processing import image_processor

    def page(text):
        img = Image.new("RGB", (1200, 300), "white")
        ImageDraw.Draw(img).text((40, 100), text, fill="black",
                                 font=ImageFont.load_default(size=60))
        return img

    f = tmp_path / "scan.tiff"
    page("INVOICE 4711").save(f, save_all=True, append_images=[page("TOTAL 99")])
    assert image_processor.count_image_frames(f) == 2
    texts = [image_processor.preprocess_and_ocr(fr).text
             for fr in image_processor.iter_image_frames(f)]
    assert "4711" in texts[0] and "99" in texts[1]
