# Database Schema & Data Models

## Entity Relationship Diagram

```mermaid
erDiagram
    users ||--o{ documents : owns
    users ||--o{ corrections : performs
    users ||--o{ crosschecks : runs
    documents ||--o{ crosschecks : compared_in
    documents ||--o{ document_pages : contains
    documents ||--o{ invoices : extracts
    documents ||--o{ validation_issues : flags
    documents ||--o{ processing_events : logs
    documents ||--o{ document_chunks : indexes
    invoices ||--o{ line_items : includes
    invoices ||--o{ validation_issues : triggers
    invoices ||--o{ corrections : audits
    invoices ||--o{ document_chunks : links

    users {
        uuid id PK
        string email UK
        string password_hash
        string role
        timestamp created_at
    }

    documents {
        uuid id PK
        uuid user_id FK
        string original_filename
        string stored_path
        string mime_type
        string file_ext
        bigint size_bytes
        string sha256
        int page_count
        string status
        float progress_percent
        string error_code
        string error_message
        string llm_provider_requested
        timestamp created_at
        timestamp updated_at
        timestamp completed_at
    }

    document_pages {
        uuid id PK
        uuid document_id FK
        int page_number
        string source_type
        text text
        float ocr_confidence
        int char_count
        string status
        text error_message
    }

    invoices {
        uuid id PK
        uuid document_id FK
        int segment_index
        int page_start
        int page_end
        string invoice_number
        date invoice_date
        string supplier_name
        string customer_name
        numeric subtotal
        numeric tax
        numeric discount
        numeric total
        string currency
        string supplier_norm
        string invoice_number_norm
        jsonb field_confidence
        jsonb field_evidence
        jsonb raw_extraction
        string provider
        string model
        string prompt_version
        int tokens_in
        int tokens_out
        int latency_ms
        boolean needs_review
        text_array review_reason
        boolean is_corrected
        timestamp created_at
        timestamp updated_at
    }

    line_items {
        uuid id PK
        uuid invoice_id FK
        int position
        text description
        numeric quantity
        numeric unit_price
        numeric line_total
        numeric tax_rate
        jsonb raw
    }

    validation_issues {
        uuid id PK
        uuid invoice_id FK
        uuid document_id FK
        string code
        string severity
        string field
        text message
        jsonb details
        boolean resolved
        timestamp created_at
    }

    corrections {
        uuid id PK
        uuid invoice_id FK
        uuid user_id FK
        string field
        text old_value
        text new_value
        timestamp created_at
    }

    crosschecks {
        uuid id PK
        uuid user_id FK
        uuid document_a_id FK
        uuid document_b_id FK
        string status
        jsonb result
        timestamp created_at
    }

    processing_events {
        uuid id PK
        uuid document_id FK
        string stage
        string status
        text message
        int duration_ms
        jsonb details
        timestamp created_at
    }

    document_chunks {
        uuid id PK
        uuid document_id FK
        uuid invoice_id FK
        int chunk_index
        text content
        tsvector tsv
        vector_768 embedding
        string embedding_provider
        string embedding_model
    }
```

## Key Indexes
- `documents(user_id, status)`: filtered document lists.
- `documents(sha256)`: duplicate file detection.
- `invoices(supplier_norm, invoice_number_norm)`: duplicate invoice detection across documents. Every query also filters by the owner through `documents.user_id`.
- `invoices(invoice_date)`, `invoices(total)`: date and amount filters.
- `document_chunks.tsv`: PostgreSQL GIN index for full-text search.
- `document_chunks.embedding`: HNSW cosine index for pgvector semantic search (`vector_cosine_ops`).

## Notes
- Money is `NUMERIC(18,4)`; the API returns two decimals.
- `crosschecks.result` holds the whole comparison (summary, pairs, only-in-A, only-in-B) as JSON, so a saved result is returned as it was computed.
- `corrections` is an audit trail: every manual edit stores the old and new value.
- `document_chunks.embedding_provider` and `embedding_model` record which model made each vector; semantic search only uses vectors from the active model.
- Migrations: `0001` pgvector extension, `0002` core tables, `0003` crosschecks, `0004` invoice discount.
