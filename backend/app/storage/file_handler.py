import hashlib
import logging
import shutil
import uuid
from pathlib import Path

import magic
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Document
from app.processing.errors import DocumentError, ErrorCode

logger = logging.getLogger(__name__)

# Allowed MIME types → canonical extension
ALLOWED_MIME_TYPES: dict[str, str] = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/tiff": "tiff",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "text/csv": "csv",
    "text/plain": "csv",  # treat plain text as csv
}

MAX_BYTES = settings.MAX_UPLOAD_MB * 1024 * 1024
UPLOAD_DIR = Path(settings.UPLOAD_DIR)


# libmagic reports these containers/generic types for some valid uploads. They are
# only accepted when the client-supplied extension agrees with the expected format.
AMBIGUOUS_MIME_BY_EXT: dict[str, tuple[str, set[str]]] = {
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             {"application/zip", "application/octet-stream"}),
    "xls": ("application/vnd.ms-excel",
            {"application/x-ole-storage", "application/CDFV2", "application/cdfv2",
             "application/octet-stream"}),
    "csv": ("text/csv", {"application/csv", "application/vnd.ms-excel", "text/x-csv"}),
    "tiff": ("image/tiff", {"image/x-tiff"}),
}
EXT_ALIASES = {"jpeg": "jpg", "tif": "tiff"}


def _detect_mime(file_path: Path) -> str:
    """Detect real MIME type from magic bytes, ignoring client extension."""
    detected = magic.from_file(str(file_path), mime=True)
    return detected


def _resolve_mime(detected: str, original_filename: str) -> str:
    """Map a libmagic result to an allowed MIME type, using the extension only to
    disambiguate generic container types (never to override a clear detection)."""
    if detected in ALLOWED_MIME_TYPES:
        return detected
    ext = Path(original_filename).suffix.lower().lstrip(".")
    ext = EXT_ALIASES.get(ext, ext)
    expected = AMBIGUOUS_MIME_BY_EXT.get(ext)
    if expected and detected in expected[1]:
        return expected[0]
    return detected


def _compute_sha256_streaming(file_path: Path) -> str:
    """Compute SHA-256 by reading the already-saved file in chunks."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def save_upload(
    file_bytes: bytes,
    original_filename: str,
    db: Session,
    user_id: uuid.UUID | str | None = None,
) -> dict:
    """
    Save uploaded file bytes, detect format, compute SHA-256, and check duplicates.
    Duplicates are matched per user when ``user_id`` is given (so one tenant's upload
    never resolves to another tenant's document), otherwise globally.

    Returns a dict with:
      - document_id (uuid str)
      - stored_path (str)
      - mime_type (str)
      - file_ext (str)
      - sha256 (str)
      - size_bytes (int)
      - is_duplicate (bool)
    """
    size_bytes = len(file_bytes)
    if size_bytes > MAX_BYTES:
        raise DocumentError(
            ErrorCode.FILE_TOO_LARGE,
            f"{size_bytes / 1024 / 1024:.1f} MB exceeds limit of {settings.MAX_UPLOAD_MB} MB",
        )

    # Write to a temp path for magic detection
    tmp_id = uuid.uuid4()
    tmp_dir = UPLOAD_DIR / str(tmp_id)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / "upload.tmp"

    try:
        tmp_path.write_bytes(file_bytes)

        # Magic byte MIME detection (ignore client extension)
        mime_type = _resolve_mime(_detect_mime(tmp_path), original_filename)
        if mime_type not in ALLOWED_MIME_TYPES:
            raise DocumentError(ErrorCode.UNSUPPORTED_TYPE, f"detected type '{mime_type}'")

        file_ext = ALLOWED_MIME_TYPES[mime_type]

        # SHA-256 for duplicate detection
        sha256 = _compute_sha256_streaming(tmp_path)

        # Check if file was already uploaded
        query = db.query(Document).filter(Document.sha256 == sha256)
        if user_id is not None:
            query = query.filter(Document.user_id == user_id)
        existing = query.first()
        if existing:
            logger.info(
                f"Duplicate file detected: sha256={sha256}, existing_id={existing.id}"
            )
            # Clean up temp
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return {
                "document_id": str(existing.id),
                "stored_path": existing.stored_path,
                "mime_type": existing.mime_type,
                "file_ext": existing.file_ext,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "is_duplicate": True,
            }

        # Move to permanent location
        doc_id = uuid.uuid4()
        dest_dir = UPLOAD_DIR / str(doc_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / f"{doc_id}.{file_ext}"
        shutil.move(str(tmp_path), str(dest_path))
        shutil.rmtree(tmp_dir, ignore_errors=True)

        logger.info(
            f"Saved upload: id={doc_id}, mime={mime_type}, "
            f"size={size_bytes / 1024:.1f}KB, sha256={sha256[:12]}..."
        )

        return {
            "document_id": str(doc_id),
            "stored_path": str(dest_path),
            "mime_type": mime_type,
            "file_ext": file_ext,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "is_duplicate": False,
        }

    except DocumentError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise DocumentError(ErrorCode.INTERNAL_ERROR, f"Failed to save upload: {e!r}") from e


def register_document(
    db: Session,
    saved: dict,
    original_filename: str,
    user_id: uuid.UUID | str,
    llm_provider: str | None = None,
) -> Document:
    """
    Create the Document row for a ``save_upload`` result. Returns the existing row
    untouched when the upload was a duplicate. The caller enqueues processing.
    """
    if saved["is_duplicate"]:
        return db.get(Document, uuid.UUID(saved["document_id"]))

    doc = Document(
        id=uuid.UUID(saved["document_id"]),
        user_id=user_id,
        original_filename=original_filename[:500],
        stored_path=saved["stored_path"],
        mime_type=saved["mime_type"],
        file_ext=saved["file_ext"],
        size_bytes=saved["size_bytes"],
        sha256=saved["sha256"],
        status="queued",
        llm_provider_requested=llm_provider,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc
