"""
Provider resolution.

LLM (extraction):
  * explicit provider (per document request, else LLM_PROVIDER=openai|gemini|fake):
    its key must exist, otherwise LLM_NOT_CONFIGURED. An explicit choice is never
    silently swapped, so there is no fallback.
  * auto: configured providers ordered by LLM_PREFERENCE; the first is primary, the
    second (if any and LLM_FALLBACK_ENABLED) is the fallback; none -> LLM_NOT_CONFIGURED.

Embeddings: EMBEDDING_PROVIDER explicit (key required, else LLM_NOT_CONFIGURED) or auto
(same preference order); ``None`` when no provider is available, so semantic search can
answer 503 while everything else keeps working.
"""
from dataclasses import dataclass

from app.core.config import settings
from app.llm.base import LLMProvider
from app.processing.errors import DocumentError, ErrorCode

KNOWN = ("openai", "gemini", "fake")


@dataclass
class Resolution:
    primary: LLMProvider
    fallback: LLMProvider | None = None


def _has_key(name: str) -> bool:
    return {"openai": settings.has_openai, "gemini": settings.has_gemini, "fake": True}[name]


def build(name: str) -> LLMProvider:
    if name == "fake":
        from app.llm.fake import FakeProvider
        return FakeProvider()
    if name == "openai":
        from app.llm.openai_provider import OpenAIProvider
        return OpenAIProvider(settings.OPENAI_API_KEY)
    if name == "gemini":
        from app.llm.gemini_provider import GeminiProvider
        return GeminiProvider(settings.GEMINI_API_KEY)
    raise DocumentError(ErrorCode.LLM_NOT_CONFIGURED, f"Unknown provider '{name}'")


def _auto_order() -> list[str]:
    prefs = [p.strip().lower() for p in settings.LLM_PREFERENCE.split(",") if p.strip()]
    order = [p for p in prefs if p in ("openai", "gemini") and _has_key(p)]
    return order + [p for p in ("openai", "gemini") if _has_key(p) and p not in order]


def _explicit(name: str) -> str:
    name = name.strip().lower()
    if name not in KNOWN or not _has_key(name):
        raise DocumentError(ErrorCode.LLM_NOT_CONFIGURED, f"Provider '{name}' has no API key")
    return name


def resolve_llm(requested: str | None = None) -> Resolution:
    choice = (requested or settings.LLM_PROVIDER or "auto").strip().lower()
    if choice != "auto":
        return Resolution(build(_explicit(choice)))
    order = _auto_order()
    if not order:
        raise DocumentError(ErrorCode.LLM_NOT_CONFIGURED, "No provider key is set")
    fallback = build(order[1]) if len(order) > 1 and settings.LLM_FALLBACK_ENABLED else None
    return Resolution(build(order[0]), fallback)


def resolve_embedder() -> LLMProvider | None:
    choice = (settings.EMBEDDING_PROVIDER or "auto").strip().lower()
    if choice != "auto":
        return build(_explicit(choice))
    order = _auto_order()
    return build(order[0]) if order else None
