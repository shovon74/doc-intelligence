import time
import uuid
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.logging import setup_logging
from app.api.v1.crosscheck import router as crosscheck_router
from app.api.v1.health import router as health_router

setup_logging()

app = FastAPI(
    title="AI-Powered Document Intelligence API",
    description="High-volume financial document processing, extraction, validation, and search.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/api/v1/openapi.json",
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    request.state.correlation_id = correlation_id
    start_time = time.time()

    response = await call_next(request)

    process_time_ms = int((time.time() - start_time) * 1000)
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["X-Process-Time-MS"] = str(process_time_ms)
    return response


# Include Routers
app.include_router(health_router, prefix="/api/v1", tags=["System & Health"])
app.include_router(crosscheck_router, prefix="/api/v1", tags=["Cross-check"])


@app.get("/", include_in_schema=False)
def root():
    return {
        "name": "AI-Powered Document Intelligence System",
        "docs": "/docs",
        "health": "/api/v1/health",
        "version": "1.0.0",
    }
