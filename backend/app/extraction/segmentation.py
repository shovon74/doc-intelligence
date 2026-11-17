"""
Heuristic invoice segmentation: one document may hold several invoices.

A new segment starts on a page whose printed invoice number differs from the current
segment's number. Pages without a number continue the current segment (multi-page
invoices). A document with no recognisable number is a single segment.
(LLM-assisted boundary checks are a deferred task.)
"""
import re
from dataclasses import dataclass

from app.extraction.normalize import norm_invoice_number

_NUMBER = re.compile(
    r"invoice\s*(?:number|no\.?|num\.?|#)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-/_.]{1,30})|"
    r"\binv(?:oice)?\s*[#:]\s*([A-Za-z0-9][A-Za-z0-9\-/_.]{1,30})", re.IGNORECASE)


@dataclass
class Segment:
    page_start: int
    page_end: int
    text: str


def page_invoice_number(text: str) -> str | None:
    m = _NUMBER.search(text)
    if not m:
        return None
    return norm_invoice_number((m.group(1) or m.group(2)).rstrip(".-/_"))


def segment_pages(pages: list[tuple[int, str]]) -> list[Segment]:
    """``pages``: (page_number, text) in order; blank pages are skipped."""
    segments: list[list] = []          # [start, end, [(page, text)], number]
    for number, text in pages:
        if not text.strip():
            continue
        found = page_invoice_number(text)
        cur = segments[-1] if segments else None
        if cur is None or (found and cur[3] and found != cur[3]):
            segments.append([number, number, [(number, text)], found])
            continue
        cur[1] = number
        cur[2].append((number, text))
        cur[3] = cur[3] or found
    return [Segment(s[0], s[1], "\n\n".join(f"[Page {n}]\n{t}" for n, t in s[2]))
            for s in segments]
