"""
Phase 2 acceptance: files go through the real intake (libmagic detection, streaming
save) and the real pipeline, and every input maps to the right result or error code.
"""
import io
import uuid
from unittest.mock import MagicMock

import fitz
import pandas as pd
import pytest
from PIL import Image, ImageDraw

from app.processing import ocr
from app.processing.errors import DocumentError, ErrorCode
from app.processing.pipeline import process_document
from app.storage import file_handler

TEXT = "Invoice INV-001 total 120.00 EUR supplier ACME Ltd " * 3


@pytest.fixture
def intake(tmp_path, monkeypatch):
    """Returns run(name, data) -> (saved, doc) after save_upload + process_document."""
    monkeypatch.setattr(file_handler, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(ocr, "_run_tesseract", lambda img, langs=None: ("ocr text 123", 0.9))
    db_up = MagicMock()
    db_up.query.return_value.filter.return_value.first.return_value = None

    def run(name: str, data: bytes):
        saved = file_handler.save_upload(io.BytesIO(data), name, db_up)
        doc = MagicMock(id=uuid.uuid4(), stored_path=saved["stored_path"],
                        file_ext=saved["file_ext"], original_filename=name)
        db = MagicMock()
        db.get.return_value = doc
        process_document(db, doc.id)
        return saved, doc

    return run


def pdf_bytes(pages=("text",), password=None) -> bytes:
    d = fitz.open()
    for t in pages:
        d.new_page().insert_textbox(fitz.Rect(40, 40, 550, 800), t, fontsize=11)
    kw = dict(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw="o") if password else {}
    out = d.tobytes(**kw)
    d.close()
    return out


def image_bytes(fmt: str) -> bytes:
    img = Image.new("RGB", (400, 200), "white")
    ImageDraw.Draw(img).text((20, 80), "INVOICE 123", fill="black")
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


# ------------------------------------------------------------ valid file of each type
@pytest.mark.unit
@pytest.mark.parametrize("name,data_fn,mime,ext", [
    ("inv.pdf", lambda: pdf_bytes([TEXT]), "application/pdf", "pdf"),
    ("scan.png", lambda: image_bytes("PNG"), "image/png", "png"),
    ("scan.jpg", lambda: image_bytes("JPEG"), "image/jpeg", "jpg"),
    ("scan.tiff", lambda: image_bytes("TIFF"), "image/tiff", "tiff"),
    ("inv.csv", lambda: b"Invoice No,Total\n001,10.5\n002,20\n", "text/csv", "csv"),
])
def test_valid_file_of_each_type_is_accepted_and_processed(intake, name, data_fn, mime, ext):
    saved, doc = intake(name, data_fn())
    assert saved["file_ext"] == ext
    assert saved["mime_type"] in (mime, "text/plain")        # libmagic reports CSV as plain text
    assert doc.status == "extracting" and doc.page_count >= 1


@pytest.mark.unit
def test_valid_xlsx_is_accepted_and_processed(intake, tmp_path):
    f = tmp_path / "inv.xlsx"
    pd.DataFrame({"Invoice No": ["001"], "Total": [10]}).to_excel(f, index=False)
    saved, doc = intake("inv.xlsx", f.read_bytes())
    assert saved["file_ext"] == "xlsx" and doc.status == "extracting"


# ------------------------------------------------------------ rejections
@pytest.mark.unit
@pytest.mark.parametrize("name,data", [
    ("invoice.pdf", b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200),   # Windows exe renamed .pdf
    ("invoice.pdf", b"PK\x03\x04" + b"\x00" * 200),                    # zip renamed .pdf
    ("invoice.xlsx", bytes(range(256)) * 4),                           # binary junk named .xlsx
    ("invoice.xls", bytes(range(256)) * 4),                            # binary junk named .xls
    ("notes.txt", b"Just some plain text, not a spreadsheet."),
    ("fake.pdf", b"Just some plain text, not a pdf."),
    ("photo.jpg", b"<html><script>alert(1)</script></html>"),          # html renamed .jpg
])
def test_renamed_fake_is_rejected_as_unsupported(intake, name, data):
    with pytest.raises(DocumentError) as ei:
        intake(name, data)
    assert ei.value.code is ErrorCode.UNSUPPORTED_TYPE


@pytest.mark.unit
def test_zero_byte_file_is_rejected_as_empty(intake):
    with pytest.raises(DocumentError) as ei:
        intake("empty.pdf", b"")
    assert ei.value.code is ErrorCode.EMPTY_FILE


@pytest.mark.unit
def test_truncated_pdf_is_corrupt(intake):
    with pytest.raises(DocumentError) as ei:
        intake("cut.pdf", pdf_bytes([TEXT])[:120])
    assert ei.value.code is ErrorCode.CORRUPT_FILE


@pytest.mark.unit
def test_encrypted_pdf_is_rejected(intake):
    with pytest.raises(DocumentError) as ei:
        intake("locked.pdf", pdf_bytes([TEXT], password="secret"))
    assert ei.value.code is ErrorCode.ENCRYPTED_PDF
