# System Architecture

## Overview
The AI-Powered Document Intelligence System is a resilient, scalable, multi-tenant document processing platform capable of processing high-volume financial documents (PDFs up to ~1,000 pages, scanned images, multi-sheet Excel files, CSVs), extracting structured financial data using a dual-provider LLM abstraction (OpenAI & Gemini), validating business and calculation logic, and providing hybrid keyword/semantic search.

## High-Level Architecture Diagram

```mermaid
flowchart TD
    subgraph Client ["Client Tier"]
        UI["React + Vite + Tailwind Web App\n(Port 3000 / 5173)"]
        CLI["API Clients / Third-party Systems"]
    end

    subgraph Gateway ["API & Ingestion Tier"]
        API["FastAPI REST API\n(Port 8000)"]
        Auth["JWT Auth & Rate Limiting"]
        Storage["Storage Interface\n(/data/uploads)"]
    end

    subgraph MessageQueue ["Async Task Queue"]
        Redis[("Redis 7\nBroker & Cache")]
        Worker["Celery Worker(s)\n(Horizontal Scaling)"]
    end

    subgraph ProcessingPipeline ["Document Pipelines"]
        Detect["Magic Byte Detection"]
        PDF["PyMuPDF Streaming & Page Batching"]
        OCR["OpenCV Preprocessing & Tesseract"]
        Spreadsheet["Pandas / openpyxl / xlrd Multi-sheet"]
    end

    subgraph LLMAbstractions ["Dual LLM Layer"]
        LLMFactory["LLM Provider Factory"]
        OpenAI["OpenAI Adapter (gpt-4o / gpt-4.1)"]
        Gemini["Gemini Adapter (gemini-2.5-flash)"]
        Embedder["Embedding Engine (dim=768)"]
    end

    subgraph Persistence ["Data & Search Tier"]
        PG[("PostgreSQL 16 + pgvector")]
        Relational["Relational Tables\n(Invoices, Line Items, Issues)"]
        FullText["PostgreSQL GIN tsvector"]
        VectorDB["HNSW pgvector (Cosine Index)"]
    end

    UI --> API
    CLI --> API
    API --> Auth
    API --> Storage
    API --> Redis
    Redis --> Worker
    Worker --> ProcessingPipeline
    Worker --> LLMAbstractions
    Worker --> Persistence
    API --> Persistence
```

## Processing Flow
1. **Intake & Hashing**: Upload streamed in chunks, SHA-256 computed on the fly, stored at `/data/uploads/<uuid>/<uuid>.<ext>`.
2. **Detection**: Magic byte verification detects file format independent of client extension.
3. **Queueing**: Asynchronous job queued in Redis via Celery. Fast 202 Accepted response returned.
4. **Processing**: Document parsed page-by-page (PyMuPDF for digital/OCR fallback, OpenCV+Tesseract for images, pandas for spreadsheets).
5. **AI Extraction**: Extracted text segmented and passed to active LLM provider (or mapped directly for structured spreadsheets) using strict Pydantic schemas.
6. **Validation Engine**: Mathematical, missing field, tax anomaly, and duplicate detection rules evaluated. Manual review flagged if necessary.
7. **Indexing**: Invoices and chunks indexed into PostgreSQL full-text tsvector and embedded with pgvector.
