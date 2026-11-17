"""
Provider-independent LLM interface.

A provider implements two small primitives (``_complete`` and ``embed``). The shared
logic here adds, for every provider: temperature-0 JSON output, retry with backoff on
transient errors only, and exactly one JSON repair attempt when the reply is not valid
JSON or fails validation.
"""
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.processing.errors import DocumentError, ErrorCode

logger = logging.getLogger(__name__)


@dataclass
class LLMResult:
    data: dict
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, DocumentError) and exc.retryable


def parse_json_object(text: str) -> dict:
    """Parse a JSON object from a model reply, tolerating markdown code fences."""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE)
    obj = json.loads(s)
    if not isinstance(obj, dict):
        raise ValueError("reply is not a JSON object")
    return obj


class LLMProvider(ABC):
    name: str
    model: str
    embedding_model: str
    retry_wait = wait_exponential(multiplier=1, max=20)

    @abstractmethod
    def _complete(self, system: str, user: str) -> tuple[str, int, int]:
        """One JSON-mode, temperature-0 call -> (reply text, tokens_in, tokens_out).

        Must raise DocumentError (LLM_RATE_LIMITED / LLM_PROVIDER_UNAVAILABLE /
        LLM_NOT_CONFIGURED / EXTRACTION_PARSE_ERROR) for provider failures."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embeddings of ``settings.EMBEDDING_DIM`` dimensions, one per text."""

    def _call(self, system: str, user: str) -> tuple[str, int, int]:
        for attempt in Retrying(stop=stop_after_attempt(max(settings.LLM_MAX_RETRIES, 1)),
                                wait=self.retry_wait, retry=retry_if_exception(_is_transient),
                                reraise=True):
            with attempt:
                return self._complete(system, user)
        raise AssertionError("unreachable")  # pragma: no cover

    def complete_json(self, system: str, user: str,
                      validate: Callable[[dict], object] | None = None) -> LLMResult:
        """JSON reply that parses and passes ``validate``; one repair attempt, then
        EXTRACTION_PARSE_ERROR."""
        t0 = time.time()
        tin = tout = 0
        prompt, last_err = user, None
        for attempt in range(2):
            text, a, b = self._call(system, prompt)
            tin, tout = tin + a, tout + b
            try:
                data = parse_json_object(text)
                if validate:
                    validate(data)
                return LLMResult(data, self.name, self.model, tin, tout,
                                 int((time.time() - t0) * 1000))
            except (ValueError, TypeError) as e:     # JSONDecodeError and ValidationError
                last_err = e
                logger.warning(f"{self.name}: invalid reply (attempt {attempt + 1}): {e}")
                prompt = (f"{user}\n\nYour previous reply was invalid ({str(e)[:300]}). "
                          "Reply again with ONLY the corrected JSON object.")
        raise DocumentError(ErrorCode.EXTRACTION_PARSE_ERROR, f"{self.name}: {last_err}")
