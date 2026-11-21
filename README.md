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
- A Gemini or OpenAI API key for live extraction and semantic search. You can paste it in the UI after sign-in (see step 2); no file editing needed.

### 2. Configure Environment
```bash
cp .env.example .env
# Optional: set GEMINI_API_KEY here. Or skip it and enter the key in the UI.
```

**Key from the UI (no config edit).** After signing in, if no key is configured, a prompt asks for the provider (Gemini or OpenAI), model name and API key. The key is checked with one test call, then saved to `/data/config/llm.json` on the `config_data` Docker volume, which lives outside the container and is shared by the API and worker. It is a one-time step that survives `docker compose down` and rebuilds. Use **AI settings** in the header to change it later. A key saved in the UI overrides `.env`. The key file is plain JSON with owner-only permissions; a production deployment would use a secrets manager.

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

**Status.** `match` when every invoice in A has a partner in B and no field differs; otherwise
`mismatch`. Invoices only in B (the rest of a ledger) are listed but never cause a mismatch.

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

**UI.** The **Ledger Check** page in the web app: pick an invoice that is already in the system (A),
upload a ledger (XLSX, XLS or CSV export from your CRM) as B, and press Check. The ledger is
processed first, then you get the status, the per-invoice difference table and the "Only in A /
Only in B" lists. An invoice that is in the ledger is paired and any differing fields are listed;
one that is not appears under "Only in A". The ledger's other rows appear under "Only in B". B can
also be an already uploaded document.

**Demo data.** `make eval` generates sample invoices into `backend/eval/dataset/` (git-ignored).
`backend/eval/demo/ledger_demo.xlsx` (also `.csv`) is a ledger built to go with them: it contains
`digital_01.pdf` and `image_01.png` plus six other invoices, and does not contain `digital_00.pdf`
or `image_00.png`. Upload the invoices, then in Ledger Check pick one as A and upload the ledger as
B: the first two pair with no differences, the last two show under "Only in A". Rebuild it with
`python eval/make_demo_ledger.py`.

---

## Reviewing and Correcting Extractions

On a document's page, every invoice-level field (number, date, supplier, customer, subtotal, tax, discount, total, currency) is editable, and the **Issues** panel lists only problems with those fields. Missing tax, discount or line items are never reported; only the vital fields (invoice number, date, supplier, customer, total, currency) raise a "missing" error.

Line items are edited in place. A line that looks wrong (for example quantity x unit price does not equal the line total, or a value was not found in the document text) is highlighted with its message directly under it, with one-click suggestions such as "Set unit price to 64.00". Use **+ Add line** for lines the extraction missed and **Delete** to remove a wrong one. Every edit is saved as a correction, marks the invoice as corrected, and re-runs validation, so the warning disappears as soon as the numbers agree.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/invoices/{id}/line-items` | Add a line. |
| `PATCH` | `/api/v1/invoices/{id}/line-items/{line_id}` | Change description, quantity, unit price or total of one line. |
| `DELETE` | `/api/v1/invoices/{id}/line-items/{line_id}` | Remove a line. |
| `POST` | `/api/v1/invoices/{id}/confirm` | Mark values flagged "not found in the document text" as checked. Body: `{"fields": ["total", "line_items[1]"]}`. |

A value flagged as not found in the document text can be left alone with **Mark as correct** (shown next to the flag, for invoice fields and for each line) when it is right; no retyping is needed. Once nothing is flagged, the invoice and then the document move out of `needs_review`.

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
