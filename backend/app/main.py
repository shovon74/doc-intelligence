import re
import time
import uuid
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.logging import setup_logging
from app.api import errors
from app.api.v1.auth import router as auth_router
from app.api.v1.crosscheck import router as crosscheck_router
from app.api.v1.documents import router as documents_router
from app.api.v1.health import router as health_router
from app.api.v1.invoices import router as invoices_router
from app.api.v1.providers import router as providers_router
from app.api.v1.search import router as search_router

setup_logging()

app = FastAPI(
    title="AI-Powered Document Intelligence API",
    description="High-volume financial document processing, extraction, validation, and search.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/api/v1/openapi.json",
)

# CORS: explicit allow-list of origins, methods and headers
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
    expose_headers=["X-Correlation-ID"],
)
errors.install(app)

_DOC_PATHS = ("/docs", "/redoc", "/api/v1/openapi.json")
_CORRELATION_OK = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@app.middleware("http")
async def correlation_and_security_headers(request: Request, call_next):
    supplied = request.headers.get("X-Correlation-ID", "")
    correlation_id = supplied if _CORRELATION_OK.match(supplied) else str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    start_time = time.time()

    response = await call_next(request)

    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["X-Process-Time-MS"] = str(int((time.time() - start_time) * 1000))
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path not in _DOC_PATHS:      # Swagger UI needs scripts from a CDN
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    if request.url.path.startswith("/api/") and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    if settings.ENVIRONMENT.lower() == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# Include Routers
app.include_router(health_router, prefix="/api/v1", tags=["System & Health"])
app.include_router(auth_router, prefix="/api/v1")
app.include_router(documents_router, prefix="/api/v1")
app.include_router(invoices_router, prefix="/api/v1")
app.include_router(crosscheck_router, prefix="/api/v1", tags=["Cross-check"])
app.include_router(providers_router, prefix="/api/v1")
app.include_router(search_router, prefix="/api/v1")


@app.get("/", include_in_schema=False)
def root():
    return {
        "name": "AI-Powered Document Intelligence System",
        "docs": "/docs",
        "health": "/api/v1/health",
        "version": "1.0.0",
    }
