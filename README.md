# AI-Powered Document Intelligence System

A production-grade, scalable document intelligence platform designed to extract, validate, and search high-volume financial documents (PDFs up to ~1,000 pages, scanned receipts, multi-sheet Excel files, CSVs).

---

## Key Features

- **Multi-Format Document Pipeline**: Accepts PDF, JPG, PNG, TIFF, XLSX, XLS, and CSV. Streaming page-by-page PyMuPDF extraction with OCR fallback for digital/scanned PDFs; OpenCV deskew/denoise + Tesseract OCR for images; multi-sheet parsing and automatic column mapping for spreadsheets.
- **Gemini-Powered Extraction**: Uses Google Gemini (`gemini-2.5-flash` by default) for extraction and embeddings, with structured output schemas (`response_schema`) so missing values stay empty instead of being invented. The model name comes from config and is shown read-only in the UI. A small provider interface keeps the LLM swappable: set `LLM_PROVIDER=openai` and `OPENAI_API_KEY` to use OpenAI instead, with no UI change.
- **Financial Validation Engine**: 11 business and mathematical validation rules with configurable tolerances, duplicate invoice detection across files, and automatic manual-review flagging.
- **PDF-PDF & PDF-Excel Cross Verification**: Reconcile invoice amounts, line items, and totals between matching documents across formats.
- **PostgreSQL 16 + pgvector Search**: Keyword search with filters (`tsvector` + GIN) and a separate semantic search mode (pgvector cosine similarity).
- **Asynchronous Background Processing**: Celery + Redis with task groups for parallel processing of ~1,000-page documents with bounded memory.
- **Responsive React Dashboard**: Inspect documents, view extracted data beside the original document, fix validation errors with instant re-validation, and perform search.

---

## Quick Start (Single-Command)

### 1. Prerequisites
- [Docker](https://www.docker.com/) & Docker Compose v2+
- A Gemini API key (`GEMINI_API_KEY`) for live extraction and semantic search (the system also operates offline for testing/scaffolding).

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env and supply your GEMINI_API_KEY
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

## Cross Verification (PDF-PDF, PDF-Excel)

Compares the invoices of two already-processed documents, for example an invoice PDF and a
CSV/XLSX ledger of the same invoices. Both sides are stored as structured invoices, so any
combination of formats works.

**Matching.** Invoices are paired by invoice number plus supplier, then by invoice number alone
(numbers are compared ignoring case and punctuation). If exactly one unpaired invoice is left on
each side they are paired anyway, so a mistyped invoice number shows up as a difference.

**Compared fields.** Invoice number, supplier, customer, date, subtotal, tax, total, currency and
line items (count, quantity, unit price, line total). Amounts use the same tolerance as validation.

**Endpoints** (JWT required, documents must be `completed` or `needs_review`):

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/crosscheck` | Run a comparison. Body: `{"document_a_id": "<uuid>", "document_b_id": "<uuid>"}`. Returns `201`. |
| `GET` | `/api/v1/crosscheck/{id}` | Fetch a saved result. |

Errors: `404` document not found, `409` document not processed yet, `422` same document twice.

```jsonc
{
  "id": "…", "document_a_id": "…", "document_b_id": "…",
  "status": "mismatch",                       // "match" | "mismatch"
  "result": {
    "summary": { "invoices_a": 1, "invoices_b": 40, "paired": 1,
                 "differences": 1, "only_in_a": 0, "only_in_b": 39 },
    "pairs": [{ "a": { "invoice_number": "INV-1001" }, "b": { "invoice_number": "INV-1001" },
                "differences": [{ "field": "total", "a": "1180.00", "b": "1108.00" }] }],
    "only_in_a": [], "only_in_b": [ /* invoices with no partner */ ]
  }
}
```

**UI.** The **Compare** page in the web app: pick two documents, press Compare, and read the
status, per-invoice difference table and the "Only in A / Only in B" lists. Tip: to check whether
one invoice exists in a ledger, put the invoice in A and the ledger in B; the ledger's other rows
appear under "Only in B".

---

## Known Limitations

- **Semantic search after an embedding outage.** Extraction and embedding are separate steps. If
  the embedding provider is unavailable while a document is indexed, the document is still saved
  and found by keyword search and filters, but its chunks have no vectors, so semantic search
  does not return it. Reprocess the document (UI: Reprocess, API: `POST /documents/{id}/reprocess`)
  to rebuild its index and restore semantic search. There is no automatic backfill job.
- **Embedding model is fixed per index.** `EMBEDDING_PROVIDER` defaults to `gemini` and should stay
  fixed. If you change the embedding provider or model, reprocess all documents.
- **Cross-verification compares two documents.** It does not search a whole ledger automatically.

---

## Architecture & Documentation
- [Architecture Details](docs/ARCHITECTURE.md)
- [Database Schema & ER Diagram](docs/DATABASE.md)
- [Evaluation Report](docs/EVALUATION.md)
- [AI Prompts & Versioning](docs/prompts.md)
- [AI-Assisted Development Disclosure](docs/AI_USAGE.md)
