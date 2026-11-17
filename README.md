# AI-Powered Document Intelligence System

A production-grade, scalable document intelligence platform designed to extract, validate, and search high-volume financial documents (PDFs up to ~1,000 pages, scanned receipts, multi-sheet Excel files, CSVs).

---

## Key Features

- **Multi-Format Document Pipeline**: Accepts PDF, JPG, PNG, TIFF, XLSX, XLS, and CSV. Streaming page-by-page PyMuPDF extraction with OCR fallback for digital/scanned PDFs; OpenCV deskew/denoise + Tesseract OCR for images; multi-sheet parsing and automatic column mapping for spreadsheets.
- **Dual LLM Provider Abstraction**: Seamless support for OpenAI (`gpt-4o-mini`, `gpt-4.1`) and Google Gemini (`gemini-2.5-flash`) with structured output schemas (`json_schema` / `response_schema`), automatic fallback, and zero hallucination guarantees.
- **Financial Validation Engine**: 11 business and mathematical validation rules with configurable tolerances, duplicate invoice detection across files, and automatic manual-review flagging.
- **PDF-PDF & PDF-Excel Cross Verification**: Reconcile invoice amounts, line items, and totals between matching documents across formats.
- **PostgreSQL 16 + pgvector Hybrid Search**: Combined full-text keyword ranking (`tsvector` + GIN) and semantic vector search (`HNSW` cosine index) with Reciprocal Rank Fusion (RRF).
- **Asynchronous Background Processing**: Celery + Redis with task groups for parallel processing of ~1,000-page documents with bounded memory.
- **Responsive React Dashboard**: Inspect documents, view extracted data beside the original document, fix validation errors with instant re-validation, and perform search.

---

## Quick Start (Single-Command)

### 1. Prerequisites
- [Docker](https://www.docker.com/) & Docker Compose v2+
- At least one LLM API key (`OPENAI_API_KEY` or `GEMINI_API_KEY`) for live extraction (the system also operates offline for testing/scaffolding).

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env and supply your GEMINI_API_KEY or OPENAI_API_KEY
```

### 3. Launch Services
```bash
docker compose up --build
```

Services will be available at:
- **Frontend Dashboard**: [http://localhost:3000](http://localhost:3000)
- **FastAPI Documentation (Swagger)**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Health Check**: [http://localhost:8000/api/v1/health](http://localhost:8000/api/v1/health)

---

## Running Tests
Run all unit and integration tests inside the Docker environment:
```bash
make test
```
Or with coverage reporting:
```bash
make test-cov
```

---

## Architecture & Documentation
- [Architecture Details](docs/ARCHITECTURE.md)
- [Database Schema & ER Diagram](docs/DATABASE.md)
- [Evaluation Report](docs/EVALUATION.md)
- [AI Prompts & Versioning](docs/prompts.md)
- [AI-Assisted Development Disclosure](docs/AI_USAGE.md)
