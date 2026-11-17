"""OpenAI adapter: JSON-mode chat completions at temperature 0, plus embeddings."""
from app.core.config import settings
from app.llm.base import LLMProvider
from app.processing.errors import DocumentError, ErrorCode


def classify_error(exc: Exception) -> DocumentError:
    """Map an openai SDK exception to a standard error code."""
    import openai

    if isinstance(exc, openai.RateLimitError):
        return DocumentError(ErrorCode.LLM_RATE_LIMITED, repr(exc))
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return DocumentError(ErrorCode.LLM_NOT_CONFIGURED, f"OpenAI key rejected: {exc!r}")
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError,
                        openai.InternalServerError)):
        return DocumentError(ErrorCode.LLM_PROVIDER_UNAVAILABLE, repr(exc))
    if isinstance(exc, openai.BadRequestError):
        return DocumentError(ErrorCode.EXTRACTION_PARSE_ERROR, repr(exc))
    return DocumentError(ErrorCode.LLM_PROVIDER_UNAVAILABLE, repr(exc))


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str | None = None,
                 embedding_model: str | None = None):
        import openai

        self.model = model or settings.OPENAI_MODEL
        self.embedding_model = embedding_model or settings.OPENAI_EMBEDDING_MODEL
        # retries are handled by LLMProvider._call (tenacity), not the SDK
        self.client = openai.OpenAI(api_key=api_key, timeout=settings.LLM_TIMEOUT_SECONDS,
                                    max_retries=0)

    def _complete(self, system: str, user: str) -> tuple[str, int, int]:
        try:
            r = self.client.chat.completions.create(
                model=self.model, temperature=0,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
            )
        except Exception as e:
            raise classify_error(e) from e
        usage = r.usage
        return (r.choices[0].message.content or "",
                getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            r = self.client.embeddings.create(model=self.embedding_model, input=texts,
                                              dimensions=settings.EMBEDDING_DIM)
        except Exception as e:
            raise classify_error(e) from e
        return [d.embedding for d in sorted(r.data, key=lambda d: d.index)]
