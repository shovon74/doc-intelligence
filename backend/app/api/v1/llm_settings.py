"""Configure the LLM provider, model and API key from the UI (no .env edit needed)."""
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, get_current_user
from app.core import runtime_config
from app.core.config import settings
from app.processing.errors import DocumentError, ErrorCode

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Providers"])

DEFAULT_MODELS = {"gemini": "gemini-2.5-flash", "openai": "gpt-4o-mini"}


class LLMSettingsIn(BaseModel):
    provider: Literal["openai", "gemini"]
    model: str = Field(min_length=1, max_length=100)
    # Omit to keep the key already stored for this provider.
    api_key: str | None = Field(default=None, max_length=500)


def _state() -> dict:
    keys = {"openai": settings.has_openai, "gemini": settings.has_gemini}
    provider = settings.LLM_PROVIDER
    models = {"openai": settings.OPENAI_MODEL, "gemini": settings.GEMINI_MODEL}
    return {
        "configured": provider == "fake" or keys.get(provider, False),
        "provider": provider if provider in keys else None,
        "model": models.get(provider),
        "has_key": keys,                       # never the key itself
        "models": models,
        "default_models": DEFAULT_MODELS,
        "source": "ui" if runtime_config.load().get("provider") else "env",
    }


def _check_key(provider: str, model: str, api_key: str) -> None:
    """One minimal call so a wrong key or model is reported now, not on the first upload."""
    try:
        if provider == "gemini":
            from app.llm.gemini_provider import GeminiProvider
            p = GeminiProvider(api_key, model=model)
        else:
            from app.llm.openai_provider import OpenAIProvider
            p = OpenAIProvider(api_key, model=model)
        p._complete("Reply with a JSON object.", '{"ping": true}')
    except DocumentError as e:
        if e.code == ErrorCode.LLM_RATE_LIMITED:        # key accepted, just throttled
            return
        cause = e.__cause__
        logger.warning(f"{provider} key check failed: {cause!r}")
        reason = getattr(cause, "message", None) or str(cause or e.message)
        if e.code == ErrorCode.LLM_PROVIDER_UNAVAILABLE and "ConnectError" in repr(cause):
            msg = f"Could not reach {provider}. Check your network and try again."
        else:
            msg = f"The {provider} key or model was rejected: {reason[:200]}"
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg)


@router.get("/settings/llm", summary="Current LLM configuration (no secrets)")
def get_llm_settings(_: CurrentUser = Depends(get_current_user)):
    return _state()


@router.post("/settings/llm", summary="Save LLM provider, model and API key")
def save_llm_settings(body: LLMSettingsIn, _: CurrentUser = Depends(get_current_user)):
    key = (body.api_key or "").strip().strip("\"'") or None
    model = body.model.strip()
    if not model:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Enter a model name.")
    existing = settings.has_gemini if body.provider == "gemini" else settings.has_openai
    if key is None and not existing:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Enter an API key.")
    effective = key or (settings.GEMINI_API_KEY if body.provider == "gemini"
                        else settings.OPENAI_API_KEY)
    _check_key(body.provider, model, effective)
    runtime_config.save(body.provider, model, key)
    return _state()
