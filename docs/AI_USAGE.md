# AI-Assisted Development Disclosure

In accordance with Deliverable 9 of the Document Intelligence System assignment:

## 1. Development Tools Used
- **Agent Environment**: Antigravity IDE (powered by Gemini models).
- **Role of the AI Assistant**:
  - Scaffolding repository layout, Docker Compose setup, and Alembic configuration.
  - Implementing document processing pipelines (PDF PyMuPDF, OCR deskew/denoise, multi-sheet Excel/CSV parsing).
  - Building Pydantic schema-constrained LLM provider abstractions for OpenAI and Gemini.
  - Implementing validation rules engine, duplicate detection, and cross-verification.
  - Designing and implementing REST APIs (FastAPI) and Celery background workers.
  - Developing React/Vite/TypeScript frontend.
  - Generating synthetic test datasets and automated test coverage.
- **Role of the Human Reviewer**:
  - Validated architectural decisions and compliance with the 5-day take-home specification.
  - Reviewed test assertions, ground-truth labels, and performance/resilience under corrupted files.
  - Supervised prompt design, anti-hallucination grounding verifications, and evaluation metrics.

## 2. Runtime AI Models & Usage
- **Extraction Providers**:
  - OpenAI: `gpt-4o-mini` (or configurable model, e.g., `gpt-4.1` / `gpt-4o`) via structured outputs (`json_schema`).
  - Google Gemini: `gemini-2.5-flash` via structured outputs (`response_schema`).
- **Embedding Models**:
  - OpenAI: `text-embedding-3-small` (dimension 768).
  - Google Gemini: `gemini-embedding-001` (dimension 768).
- **Guardrails**:
  - Strict zero-shot structured extraction prompts forbidding value invention or guessing.
  - Deterministic grounding validator verifying field evidence substrings against raw text.
  - Strict fallback to `null` on missing/uncertain information.

## 3. Phase Log
- **Phase 0 — Scaffold & Infrastructure**: Completed and verified.
  - Setup multi-container environment (PostgreSQL 16 + pgvector, Redis 7, FastAPI 0.115, Celery 5.4, Vite/React 18/TypeScript/Tailwind).
  - Configured Alembic migration `0001_create_pgvector_extension` and verified pgvector extension activation.
  - Implemented `/api/v1/health` verifying DB connectivity, Redis ping, Tesseract 5.5.0, and dynamic LLM provider status.
  - Created initial test harness (`conftest.py`, `test_health.py`) running via `pytest` inside the Docker environment.
  - All acceptance criteria passed (HTTP 200 on `/api/v1/health`, `/docs`, and frontend).
