from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, get_current_user
from app.core.config import settings
from app.llm.factory import resolve_embedder
from app.processing.errors import DocumentError
from app.schemas.api import ProvidersOut

router = APIRouter(tags=["Providers"])


@router.get("/providers", response_model=ProvidersOut,
            summary="Which LLM and embedding providers are available")
def providers(_: CurrentUser = Depends(get_current_user)):
    configured = settings.configured_providers
    try:
        embedder = resolve_embedder()
    except DocumentError:
        embedder = None
    # "auto" is not listed: the UI's "Default" choice already means "use the server setting".
    options = list(configured)
    if settings.ENVIRONMENT.lower() != "production" and settings.LLM_PROVIDER == "fake":
        options.append("fake")
    return ProvidersOut(
        llm={"configured": configured, "default": settings.LLM_PROVIDER, "options": options,
             "fallback_enabled": settings.LLM_FALLBACK_ENABLED,
             "models": {"openai": settings.OPENAI_MODEL, "gemini": settings.GEMINI_MODEL}},
        embedding={"available": embedder is not None,
                   "provider": embedder.name if embedder else None,
                   "model": embedder.embedding_model if embedder else None},
    )
