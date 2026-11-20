"""Gemini adapter (google-genai): JSON output at temperature 0, plus embeddings."""
from app.core.config import settings
from app.llm.base import LLMProvider
from app.processing.errors import DocumentError, ErrorCode


def classify_error(exc: Exception) -> DocumentError:
    """Map a google-genai / transport exception to a standard error code."""
    code = getattr(exc, "code", None)
    if code == 429:
        return DocumentError(ErrorCode.LLM_RATE_LIMITED, repr(exc))
    if code in (401, 403):
        return DocumentError(ErrorCode.LLM_NOT_CONFIGURED, f"Gemini key rejected: {exc!r}")
    if code == 400:
        return DocumentError(ErrorCode.EXTRACTION_PARSE_ERROR, repr(exc))
    return DocumentError(ErrorCode.LLM_PROVIDER_UNAVAILABLE, repr(exc))   # 5xx, network, timeout


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str | None = None,
                 embedding_model: str | None = None):
        from google import genai
        from google.genai import types

        self.model = model or settings.GEMINI_MODEL
        self.embedding_model = embedding_model or settings.GEMINI_EMBEDDING_MODEL
        self._types = types
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(api_version=settings.GEMINI_API_VERSION,
                                             timeout=settings.LLM_TIMEOUT_SECONDS * 1000))

    def _complete(self, system: str, user: str) -> tuple[str, int, int]:
        t = self._types
        try:
            r = self.client.models.generate_content(
                model=self.model, contents=user,
                config=t.GenerateContentConfig(system_instruction=system, temperature=0,
                                               response_mime_type="application/json"))
        except Exception as e:
            raise classify_error(e) from e
        usage = r.usage_metadata
        return (r.text or "", getattr(usage, "prompt_token_count", 0) or 0,
                getattr(usage, "candidates_token_count", 0) or 0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        t = self._types
        try:
            r = self.client.models.embed_content(
                model=self.embedding_model, contents=texts,
                config=t.EmbedContentConfig(output_dimensionality=settings.EMBEDDING_DIM))
        except Exception as e:
            raise classify_error(e) from e
        return [list(e.values) for e in r.embeddings]
