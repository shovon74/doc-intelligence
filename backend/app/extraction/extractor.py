"""Extract one invoice from one text segment with an LLM provider, then normalise and ground it."""
from dataclasses import dataclass, field

from app.extraction import prompts
from app.extraction.grounding import ungrounded_fields
from app.extraction.normalize import normalize_invoice
from app.llm.base import LLMProvider
from app.schemas.extraction import InvoiceExtraction, LLMInvoice

UNGROUNDED_CONFIDENCE_CAP = 0.2


@dataclass
class ExtractedInvoice:
    extraction: InvoiceExtraction
    field_confidence: dict[str, float] = field(default_factory=dict)
    field_evidence: dict[str, str | None] = field(default_factory=dict)
    ungrounded: list[str] = field(default_factory=list)
    page_start: int = 1
    page_end: int = 1
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    raw: dict = field(default_factory=dict)


def extract_invoice(provider: LLMProvider, text: str,
                    page_start: int = 1, page_end: int = 1) -> ExtractedInvoice:
    res = provider.complete_json(prompts.EXTRACTION_SYSTEM, prompts.extraction_user(text),
                                 validate=LLMInvoice.model_validate)
    raw = LLMInvoice.model_validate(res.data)
    inv = normalize_invoice(raw)
    bad = ungrounded_fields(inv, text)
    conf = dict(raw.field_confidence)
    for f in bad:
        conf[f] = min(conf.get(f, 1.0), UNGROUNDED_CONFIDENCE_CAP)
    return ExtractedInvoice(
        extraction=inv, field_confidence=conf, field_evidence=dict(raw.field_evidence),
        ungrounded=bad, page_start=page_start, page_end=page_end, provider=res.provider,
        model=res.model, prompt_version=prompts.EXTRACTION_PROMPT_VERSION,
        tokens_in=res.tokens_in, tokens_out=res.tokens_out, latency_ms=res.latency_ms,
        raw=res.data)


def llm_column_mapper(provider: LLMProvider):
    """Mapper for ``sheet_mapping.extract_from_rows``: {column: field} via the LLM."""
    def mapper(unmapped: list[str], sample_rows: list[dict]) -> dict[str, str]:
        res = provider.complete_json(prompts.COLUMN_MAPPING_SYSTEM,
                                     prompts.column_mapping_user(unmapped, sample_rows))
        # the prompt asks for {field: column}; the hook wants {column: field}
        return {col: f for f, col in res.data.items()
                if isinstance(col, str) and col in unmapped and isinstance(f, str)}
    return mapper
