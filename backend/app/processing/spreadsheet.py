"""
Spreadsheet ingestion (XLSX / XLS / CSV).

Every sheet becomes one "page" (source_type="spreadsheet") whose text is a
readable, pipe-delimited rendering used for search/indexing. The structured
rows are also returned so the extraction stage can map columns to invoice
fields directly instead of sending the sheet through an LLM.
"""
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Generator

import pandas as pd
from charset_normalizer import from_path

from app.core.config import settings
from app.processing.errors import DocumentError, ErrorCode
from app.processing.sheet_mapping import LlmMapper, SheetExtraction, count_header_matches, extract_from_rows

logger = logging.getLogger(__name__)

MAX_TEXT_ROWS = 5000   # rows rendered into the searchable text of a single sheet
HEADER_SCAN_ROWS = 30  # how far down to look for the header row
CSV_MAX_COLUMNS = 100  # fixed column names let ragged CSV rows (title lines) parse


@dataclass
class SheetResult:
    page_number: int                 # 1-indexed sheet position
    sheet_name: str
    text: str
    source_type: str = "spreadsheet"
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)   # JSON-safe records
    row_count: int = 0
    header_row: int = 0              # 0-indexed position of the detected header row
    extraction: SheetExtraction | None = None   # heuristic invoice mapping
    char_count: int = 0
    duration_ms: int = 0
    error: str | None = None

    def __post_init__(self):
        self.char_count = len(self.text)


def _clean_headers(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise headers: strip, fill blanks, de-duplicate."""
    seen: dict[str, int] = {}
    cols = []
    for i, c in enumerate(df.columns):
        name = str(c).strip() if not str(c).startswith("Unnamed:") else ""
        name = name or f"column_{i + 1}"
        n = seen.get(name, 0)
        seen[name] = n + 1
        cols.append(name if n == 0 else f"{name}_{n + 1}")
    df = df.copy()
    df.columns = cols
    return df


def detect_header_row(raw: pd.DataFrame) -> int:
    """
    Index of the header row in a header-less frame. Picks the row (within the first
    HEADER_SCAN_ROWS) with the most recognisable invoice column names; if no row has
    at least two, the first row is the header. Rows above it (titles) are discarded.
    """
    best, best_score = 0, 1
    for i in range(min(len(raw), HEADER_SCAN_ROWS)):
        score = count_header_matches(raw.iloc[i].tolist())
        if score > best_score:
            best, best_score = i, score
    return best


def _clean_frame(raw: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Header-less frame -> (frame with detected header applied, header row index)."""
    raw = raw.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    if raw.empty:
        return raw, 0
    h = detect_header_row(raw)
    body = raw.iloc[h + 1:].copy()
    body.columns = [None if pd.isna(v) else v for v in raw.iloc[h].tolist()]
    return _clean_headers(body).reset_index(drop=True), h


def _json_safe_records(df: pd.DataFrame) -> list[dict]:
    """Records with NaN -> None and timestamps -> ISO strings (JSONB-safe)."""
    out = []
    for rec in df.to_dict(orient="records"):
        clean = {}
        for k, v in rec.items():
            if pd.isna(v):
                clean[k] = None
            elif isinstance(v, datetime):    # also covers pd.Timestamp
                clean[k] = v.date().isoformat() if v.time() == datetime.min.time() else v.isoformat()
            elif isinstance(v, date):
                clean[k] = v.isoformat()
            elif hasattr(v, "item"):          # numpy scalar -> python scalar
                clean[k] = v.item()
            else:
                clean[k] = v
        out.append(clean)
    return out


def _render_text(sheet_name: str, df: pd.DataFrame) -> str:
    lines = [f"Sheet: {sheet_name}", " | ".join(df.columns)]
    for rec in df.head(MAX_TEXT_ROWS).itertuples(index=False):
        lines.append(" | ".join("" if pd.isna(v) else str(v) for v in rec))
    if len(df) > MAX_TEXT_ROWS:
        lines.append(f"... ({len(df) - MAX_TEXT_ROWS} more rows)")
    return "\n".join(lines)


def _check_row_limit(rows: int, where: str) -> None:
    if rows > settings.MAX_SHEET_ROWS:
        raise DocumentError(
            ErrorCode.FILE_TOO_LARGE,
            f"{where} has more than {settings.MAX_SHEET_ROWS} rows",
        )


def _sheet_result(page_number: int, sheet_name: str, raw: pd.DataFrame, t0: float,
                  llm_mapper: LlmMapper | None = None) -> SheetResult:
    df, header_row = _clean_frame(raw)
    rows = _json_safe_records(df)
    return SheetResult(
        page_number=page_number,
        sheet_name=sheet_name,
        text=_render_text(sheet_name, df) if not df.empty else "",   # empty sheet has no text
        columns=list(df.columns),
        rows=rows,
        row_count=len(df),
        header_row=header_row,
        extraction=extract_from_rows(list(df.columns), rows, llm_mapper, sheet_name) if rows else None,
        duration_ms=int((time.time() - t0) * 1000),
    )


def _detect_encoding(file_path: Path) -> str:
    best = from_path(str(file_path)).best()
    return best.encoding if best else "utf-8"


def _is_blank(file_path: Path, encoding: str) -> bool:
    """True if the file holds only whitespace. Stops at the first non-blank line."""
    with open(file_path, encoding=encoding, errors="replace") as f:
        return not any(line.strip() for line in f)


def _sniff_delimiter(file_path: Path, encoding: str) -> str:
    """
    Pick the delimiter that splits the most lines into the same number of fields. Robust to
    title lines above the header, which fool csv.Sniffer.
    """
    with open(file_path, encoding=encoding, errors="replace") as f:
        lines = [ln for ln in (next(f, "") for _ in range(60)) if ln.strip()]
    best, best_score = ",", 0
    for d in (",", ";", "	", "|"):
        counts = [ln.count(d) for ln in lines]
        modal = max(set(counts), key=counts.count) if counts else 0
        score = modal * counts.count(modal) if modal else 0
        if score > best_score:
            best, best_score = d, score
    return best


def _read_csv(file_path: Path) -> pd.DataFrame:
    encoding = _detect_encoding(file_path)
    if _is_blank(file_path, encoding):
        raise DocumentError(ErrorCode.EMPTY_FILE, "CSV contains only whitespace")
    try:
        # read one row past the limit: enough to detect an oversized sheet, bounded memory
        df = pd.read_csv(file_path, sep=_sniff_delimiter(file_path, encoding),
                         encoding=encoding, dtype=str,
                         header=None, names=range(CSV_MAX_COLUMNS),
                         nrows=settings.MAX_SHEET_ROWS + 1 + HEADER_SCAN_ROWS)
    except pd.errors.EmptyDataError as e:
        raise DocumentError(ErrorCode.EMPTY_FILE, f"CSV has no data: {e}") from e
    except Exception as e:
        raise DocumentError(ErrorCode.CORRUPT_FILE, f"Cannot parse CSV: {e}") from e
    _check_row_limit(len(df.dropna(how="all")) - 1, file_path.name)   # minus the header row
    return df


def stream_sheets(file_path: Path, file_ext: str,
                  llm_mapper: LlmMapper | None = None) -> Generator[SheetResult, None, None]:
    """
    Yield one SheetResult per sheet (one for CSV). A sheet that fails to parse is
    yielded with ``error`` set rather than aborting the remaining sheets.
    """
    ext = file_ext.lower().lstrip(".")
    if ext == "csv":
        t0 = time.time()
        yield _sheet_result(1, file_path.stem, _read_csv(file_path), t0, llm_mapper)
        return

    if ext not in ("xlsx", "xls"):
        raise DocumentError(ErrorCode.UNSUPPORTED_TYPE, f"Unsupported spreadsheet type: {file_ext}")

    engine = "openpyxl" if ext == "xlsx" else "xlrd"
    try:
        xls = pd.ExcelFile(file_path, engine=engine)
    except Exception as e:
        raise DocumentError(ErrorCode.CORRUPT_FILE, f"Cannot open workbook: {e}") from e
    if len(xls.sheet_names) > settings.MAX_PAGES:
        xls.close()
        raise DocumentError(ErrorCode.FILE_TOO_LARGE, f"{len(xls.sheet_names)} sheets")

    try:
        for idx, name in enumerate(xls.sheet_names, start=1):
            t0 = time.time()
            try:
                # dtype=object keeps invoice numbers like "00123" intact
                raw = pd.read_excel(xls, sheet_name=name, dtype=object, header=None,
                                    nrows=settings.MAX_SHEET_ROWS + 1 + HEADER_SCAN_ROWS)
                _check_row_limit(len(raw.dropna(how="all")) - 1, f"sheet '{name}'")
                yield _sheet_result(idx, name, raw, t0, llm_mapper)
            except DocumentError:
                raise
            except Exception as e:
                logger.warning(f"Failed to parse sheet '{name}': {e}")
                yield SheetResult(
                    page_number=idx, sheet_name=name, text="",
                    duration_ms=int((time.time() - t0) * 1000), error=str(e),
                )
    finally:
        xls.close()


def count_sheets(file_path: Path, file_ext: str) -> int:
    if file_ext.lower().lstrip(".") == "csv":
        return 1
    engine = "openpyxl" if file_ext.lower().lstrip(".") == "xlsx" else "xlrd"
    with pd.ExcelFile(file_path, engine=engine) as xls:
        return len(xls.sheet_names)
