# AI-Assisted Development Disclosure

This covers the AI prompts and the disclosure of AI-assisted development for this project.

## 1. Development tools

- **Claude Code** (Anthropic) was the main coding assistant for the backend, frontend, tests, evaluation tooling and documentation.
- **Antigravity IDE** (Gemini models) was used for the first scaffolding phase.

### What the AI assistant did
- Wrote most of the code: Docker setup, processing pipelines (PDF, OCR, spreadsheets), LLM provider layer, extraction and grounding, validation rules, REST API, Celery tasks, search, React frontend.
- Wrote the automated tests and the synthetic evaluation dataset generator.
- Drafted the documentation in this repository.

### What the human did
- Set the scope, priorities and the work-package order, and decided what to cut or defer.
- Chose the design: Gemini as the default provider, manual start with consent, the Ledger Check feature, which fields count as vital.
- Ran the application, the tests and the evaluation, and reported problems back. Several bugs were found this way, for example:
  - OCR failed on every scan and image under OpenCV 5.
  - Correct line items were flagged "not found in the document text".
  - Extra rows in a ledger made a Ledger Check a mismatch.
  - The evaluation script never started documents after upload (processing had become a manual step).
- Reviewed the evaluation results and decided how to report them, including the limits of a 10-document sample.

## 2. Runtime AI models

| Use | Model | Notes |
|---|---|---|
| Extraction (default) | Google `gemini-2.5-flash` | Set with `GEMINI_MODEL` or in the UI. |
| Extraction (optional) | OpenAI `gpt-4o-mini` | Set `LLM_PROVIDER=openai` or choose it in the UI. The evaluation in `docs/EVALUATION.md` used this model. |
| Embeddings (default) | Google `gemini-embedding-001`, 768 dims | `EMBEDDING_PROVIDER` should stay fixed. |
| Embeddings (optional) | OpenAI `text-embedding-3-small`, 768 dims | |
| Tests | Built-in fake provider and fake embedder | No network or key needed. |

Document text is sent to the chosen provider only after the user starts processing.

## 3. Guardrails against wrong AI output

- The prompt forbids guessing: a missing or ambiguous value must be `null`.
- Every extracted value must come with a verbatim snippet. A deterministic grounding check confirms the snippet appears in the document text; otherwise the value gets low confidence and a review flag.
- Arithmetic is never done by the model. Validation rules in code check totals, line sums and line math.
- Anything doubtful is flagged for manual review, and every manual edit is stored as a correction.
- The model is told to treat document text as data and to ignore instructions inside it.
- The model is called at temperature 0 in JSON mode, and the reply is validated.

## 4. Prompts
The runtime prompts and a summary of the development prompts are in [prompts.md](prompts.md).

## 5. Work log

| Work package | Content |
|---|---|
| Scaffold, database | Multi-container setup, health checks, models and migrations. |
| WP1 Processing | Error codes, PDF/OCR/spreadsheet pipelines, streaming upload, limits, page batching, retries. |
| WP2 Extraction | LLM provider layer, fake provider, extraction, grounding. |
| WP3 Validation | Validation rules, duplicate detection, cross-check API. |
| WP4 API | REST endpoints, JWT auth, security hardening. |
| WP5 Search | Indexing, keyword and semantic search. |
| WP6 Frontend | Login, upload, dashboard, detail editing, search. |
| WP7 Tests | Gap-filling tests; 225 tests, 95% coverage (`docs/test-results/`). |
| WP8 Evaluation | Dataset generator, runner, report (`docs/EVALUATION.md`). |
| Later changes | Manual start, discount-aware totals, UI-configured LLM key, inline line-item editing, Ledger Check and demo ledger. |
| WP9 Documentation | README, architecture, database, prompts, this disclosure. |
