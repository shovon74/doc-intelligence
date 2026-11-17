"""
Deterministic offline provider for tests and key-less demos.

It "extracts" only fields printed as ``Label: value`` lines and returns ``null`` for
everything else, so it never invents data. Embeddings are hash-based and stable.
"""
import hashlib
import json
import math
import re

from app.core.config import settings
from app.llm.base import LLMProvider

_LABELS = {
    "invoice_number": r"invoice\s*(?:number|no\.?|#)",
    "invoice_date": r"(?:invoice\s*)?date",
    "supplier_name": r"(?:supplier|vendor|from)",
    "customer_name": r"(?:customer|bill\s*to|client)",
    "subtotal": r"subtotal",
    "tax": r"(?:tax|vat)",
    "total": r"(?:grand\s*)?total",
    "currency": r"currency",
}


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, canned: dict | list | None = None, model: str = "fake-1"):
        """``canned``: a dict (or a list used round-robin) returned verbatim instead of parsing."""
        self.model = model
        self.embedding_model = "fake-embed-1"
        self.canned = canned
        self.calls = 0
        self.prompts: list[str] = []

    def _complete(self, system: str, user: str) -> tuple[str, int, int]:
        self.calls += 1
        self.prompts.append(user)
        if self.canned is not None:
            reply = self.canned[(self.calls - 1) % len(self.canned)] \
                if isinstance(self.canned, list) else self.canned
            reply = reply if isinstance(reply, str) else json.dumps(reply)
        else:
            reply = json.dumps(self._parse(user))
        return reply, len(user) // 4, len(reply) // 4

    @staticmethod
    def _parse(text: str) -> dict:
        out: dict = {"field_confidence": {}, "field_evidence": {}, "line_items": []}
        for field, label in _LABELS.items():
            m = re.search(rf"^\s*{label}\s*[:#]\s*(.+?)\s*$", text, re.IGNORECASE | re.MULTILINE)
            out[field] = m.group(1) if m else None
            if m:
                out["field_confidence"][field] = 0.9
                out["field_evidence"][field] = m.group(0).strip()
        return out

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for t in texts:
            vec = [0.0] * settings.EMBEDDING_DIM
            for word in re.findall(r"\w+", t.lower()):
                h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
                vec[h % settings.EMBEDDING_DIM] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors
