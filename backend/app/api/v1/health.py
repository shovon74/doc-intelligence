import shutil
import subprocess
from typing import Any, Dict
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
import redis

from app.core.config import settings
from app.db.session import get_db

router = APIRouter()


# Cache tesseract version after first successful resolution
_tesseract_version_cache: str | None = None


@router.get("/health", summary="System Health Check")
def health_check(db: Session = Depends(get_db)) -> Dict[str, Any]:
    global _tesseract_version_cache
    health_status: Dict[str, Any] = {
        "status": "ok",
        "database": "unknown",
        "redis": "unknown",
        "tesseract": "unknown",
        "llm": {
            "status": "not_configured",
            "active_provider": settings.LLM_PROVIDER,
            "configured_providers": settings.configured_providers,
            "fallback_enabled": settings.LLM_FALLBACK_ENABLED,
        },
    }

    is_healthy = True

    # 1. Check Database
    try:
        db.execute(text("SELECT 1"))
        health_status["database"] = "connected"
    except Exception as e:
        health_status["database"] = f"error: {str(e)}"
        is_healthy = False

    # 2. Check Redis
    try:
        r = redis.Redis.from_url(settings.REDIS_URL, socket_timeout=2)
        if r.ping():
            health_status["redis"] = "connected"
        else:
            health_status["redis"] = "disconnected"
            is_healthy = False
    except Exception as e:
        health_status["redis"] = f"error: {str(e)}"
        is_healthy = False

    # 3. Check Tesseract OCR
    if _tesseract_version_cache:
        health_status["tesseract"] = _tesseract_version_cache
    else:
        tesseract_bin = shutil.which("tesseract")
        if tesseract_bin:
            try:
                res = subprocess.run(
                    ["tesseract", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                first_line = res.stdout.splitlines()[0] if res.stdout else "available"
                _tesseract_version_cache = first_line
                health_status["tesseract"] = first_line
            except Exception as e:
                health_status["tesseract"] = f"error: {str(e)}"
        else:
            health_status["tesseract"] = "not_found"

    # 4. Check LLM Configuration
    if settings.configured_providers:
        health_status["llm"]["status"] = "configured"
    else:
        health_status["llm"]["status"] = "not_configured"

    http_code = status.HTTP_200_OK if is_healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=http_code, content=health_status)
