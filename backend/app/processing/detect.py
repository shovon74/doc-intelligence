"""File-type detection by magic bytes (never trusts the client extension)."""
from pathlib import Path

import magic

# Allowed MIME types → canonical extension
ALLOWED_MIME_TYPES: dict[str, str] = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/tiff": "tiff",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "text/csv": "csv",
}

# libmagic reports these containers/generic types for some valid uploads. They are
# only accepted when the client-supplied extension agrees with the expected format.
AMBIGUOUS_MIME_BY_EXT: dict[str, tuple[str, set[str]]] = {
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             {"application/zip"}),
    "xls": ("application/vnd.ms-excel",
            {"application/x-ole-storage", "application/CDFV2", "application/cdfv2"}),
    "csv": ("text/csv", {"application/csv", "application/vnd.ms-excel", "text/x-csv",
                         "text/plain"}),
    "tiff": ("image/tiff", {"image/x-tiff"}),
}
EXT_ALIASES = {"jpeg": "jpg", "tif": "tiff", "tsv": "csv"}


def detect_mime(file_path: Path) -> str:
    """Detect real MIME type from magic bytes, ignoring client extension."""
    detected = magic.from_file(str(file_path), mime=True)
    return detected


def resolve_mime(detected: str, original_filename: str) -> str:
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
