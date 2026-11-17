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
from pathlib import Path
from typing import Generator

import pandas as pd
from charset_normalizer import from_path

logger = logging.getLogger(__name__)

MAX_TEXT_ROWS = 5000   # rows rendered into the searchable text of a single sheet


@dataclass
class SheetResult:
    page_number: int                 # 1-indexed sheet position
    sheet_name: str
    text: str
    source_type: str = "spreadsheet"
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)   # JSON-safe records
    row_count: int = 0
    char_count: int = 0
    duration_ms: int = 0
    error: str | None = None

    def __post_init__(self):
        self.char_count = len(self.text)


class SpreadsheetProcessingError(Exception):
    pass


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


def _clean_frame(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(how="all").dropna(axis=1, how="all")
    df = _clean_headers(df)
    return df.reset_index(drop=True)


def _json_safe_records(df: pd.DataFrame) -> list[dict]:
    """Records with NaN -> None and timestamps -> ISO strings (JSONB-safe)."""
    out = []
    for rec in df.to_dict(orient="records"):
        clean = {}
        for k, v in rec.items():
            if pd.isna(v):
                clean[k] = None
            elif isinstance(v, pd.Timestamp):
                clean[k] = v.date().isoformat() if v.time() == pd.Timestamp(0).time() else v.isoformat()
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


def _sheet_result(page_number: int, sheet_name: str, raw: pd.DataFrame, t0: float) -> SheetResult:
    df = _clean_frame(raw)
    return SheetResult(
        page_number=page_number,
        sheet_name=sheet_name,
        text=_render_text(sheet_name, df) if not df.empty else f"Sheet: {sheet_name}",
        columns=list(df.columns),
        rows=_json_safe_records(df),
        row_count=len(df),
        duration_ms=int((time.time() - t0) * 1000),
    )


def _detect_encoding(file_path: Path) -> str:
    best = from_path(str(file_path)).best()
    return best.encoding if best else "utf-8"


def _read_csv(file_path: Path) -> pd.DataFrame:
    encoding = _detect_encoding(file_path)
    try:
        # sep=None + python engine sniffs , ; \t | delimiters
        return pd.read_csv(file_path, sep=None, engine="python", encoding=encoding, dtype=str)
    except Exception as e:
        raise SpreadsheetProcessingError(f"Cannot parse CSV: {e}") from e


def stream_sheets(file_path: Path, file_ext: str) -> Generator[SheetResult, None, None]:
    """
    Yield one SheetResult per sheet (one for CSV). A sheet that fails to parse is
    yielded with ``error`` set rather than aborting the remaining sheets.
    """
    ext = file_ext.lower().lstrip(".")
    if ext == "csv":
        t0 = time.time()
        yield _sheet_result(1, file_path.stem, _read_csv(file_path), t0)
        return

    if ext not in ("xlsx", "xls"):
        raise SpreadsheetProcessingError(f"Unsupported spreadsheet type: {file_ext}")

    engine = "openpyxl" if ext == "xlsx" else "xlrd"
    try:
        xls = pd.ExcelFile(file_path, engine=engine)
    except Exception as e:
        raise SpreadsheetProcessingError(f"Cannot open workbook: {e}") from e

    try:
        for idx, name in enumerate(xls.sheet_names, start=1):
            t0 = time.time()
            try:
                # dtype=object keeps invoice numbers like "00123" intact
                raw = pd.read_excel(xls, sheet_name=name, dtype=object)
                yield _sheet_result(idx, name, raw, t0)
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
