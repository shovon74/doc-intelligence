import hashlib
import logging
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Document
from app.processing.detect import ALLOWED_MIME_TYPES
from app.processing.detect import detect_mime as _detect_mime
from app.processing.detect import resolve_mime as _resolve_mime
from app.processing.errors import DocumentError, ErrorCode

logger = logging.getLogger(__name__)

MAX_BYTES = settings.MAX_UPLOAD_MB * 1024 * 1024
UPLOAD_DIR = Path(settings.UPLOAD_DIR)


CHUNK_SIZE = 64 * 1024


def save_upload(
    fileobj: BinaryIO,
    original_filename: str,
    db: Session,
    user_id: uuid.UUID | str | None = None,
) -> dict:
    """
    Stream an uploaded file-like object to disk in chunks, hashing (SHA-256) and
    enforcing the size limit while streaming, so the upload is never held in memory.
    Then detect the real format and check for duplicates.
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
    tmp_id = uuid.uuid4()
    tmp_dir = UPLOAD_DIR / str(tmp_id)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / "upload.tmp"

    try:
        hasher = hashlib.sha256()
        size_bytes = 0
        with open(tmp_path, "wb") as out:
            while chunk := fileobj.read(CHUNK_SIZE):
                size_bytes += len(chunk)
                if size_bytes > MAX_BYTES:
                    raise DocumentError(
                        ErrorCode.FILE_TOO_LARGE,
                        f"exceeds limit of {settings.MAX_UPLOAD_MB} MB",
                    )
                hasher.update(chunk)
                out.write(chunk)
        if size_bytes == 0:
            raise DocumentError(ErrorCode.EMPTY_FILE, f"zero-byte upload '{original_filename}'")
        sha256 = hasher.hexdigest()

        # Magic byte MIME detection (ignore client extension)
        mime_type = _resolve_mime(_detect_mime(tmp_path), original_filename)
        if mime_type not in ALLOWED_MIME_TYPES:
            raise DocumentError(ErrorCode.UNSUPPORTED_TYPE, f"detected type '{mime_type}'")

        file_ext = ALLOWED_MIME_TYPES[mime_type]

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
        status="uploaded",
        llm_provider_requested=llm_provider,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc
