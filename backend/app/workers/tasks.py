import logging

from celery import chord, group

from app.workers.celery_app import celery_app
from app.workers.retry import backoff_seconds, should_retry, will_retry

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.tasks.ping")
def ping_task():
    return {"status": "pong"}


@celery_app.task(name="app.workers.tasks.process_document", bind=True)
def process_document_task(self, document_id: str):
    """
    Run the processing stage (text extraction / OCR) for one uploaded document.

    Small files run in this task. A PDF larger than one batch is split into
    PAGE_BATCH_SIZE-page batches that run in parallel as a Celery chord; the join
    step (``finish_document_task``) sets the final status.
    Retryable errors are retried with backoff; permanent errors fail immediately.
    """
    from app.db.session import SessionLocal
    from app.processing import pipeline
    from app.processing.errors import DocumentError

    db = SessionLocal()
    try:
        retries = self.request.retries
        try:
            plan = pipeline.start_document(db, document_id, will_retry(retries))
            if not pipeline.is_batchable(plan.ext, plan.total):
                result = pipeline.process_planned(
                    db, document_id, plan,
                    on_progress=lambda pct: self.update_state(state="PROGRESS",
                                                              meta={"progress": pct}),
                    will_retry=will_retry(retries),
                )
                extract_document_task.delay(document_id)       # next stage
                return result
        except DocumentError as err:
            if should_retry(err, retries):
                raise self.retry(exc=err, countdown=backoff_seconds(retries))
            raise

        batches = [process_pages_task.s(document_id, a, b, plan.total)
                   for a, b in pipeline.batch_ranges(plan.total)]
        chord(group(batches))(finish_document_task.s(document_id))
        return {"document_id": document_id, "batches": len(batches), "dispatched": True}
    finally:
        db.close()


@celery_app.task(name="app.workers.tasks.process_pages")
def process_pages_task(document_id: str, first: int, last: int, total: int):
    """One page batch. Never raises: a crashed batch is reported as all-pages-failed."""
    from app.db.session import SessionLocal
    from app.processing import pipeline

    db = SessionLocal()
    try:
        return pipeline.process_pages(db, document_id, first, last, total, shared_progress=True)
    except Exception:
        logger.exception(f"Batch {first}-{last} of document {document_id} crashed")
        db.rollback()
        return pipeline.failed_batch_result(first, last)
    finally:
        db.close()


@celery_app.task(name="app.workers.tasks.finish_document")
def finish_document_task(results: list[dict], document_id: str):
    """Join step of the batch chord: decide the document's final status."""
    from app.db.session import SessionLocal
    from app.processing import pipeline
    from app.processing.errors import DocumentError

    db = SessionLocal()
    try:
        out = pipeline.finish_document(db, document_id, results)
        extract_document_task.delay(document_id)               # next stage
        return out
    except DocumentError as err:
        return {"document_id": document_id, "failed": True, "code": err.code.value}
    finally:
        db.close()


@celery_app.task(name="app.workers.tasks.extract_document", bind=True)
def extract_document_task(self, document_id: str):
    """Extraction stage: pages -> invoices. Retryable provider errors retry with backoff."""
    from app.db.session import SessionLocal
    from app.extraction.stage import extract_document
    from app.processing.errors import DocumentError

    db = SessionLocal()
    try:
        retries = self.request.retries
        try:
            result = extract_document(db, document_id, will_retry=will_retry(retries))
            validate_document_task.delay(document_id)              # next stage
            return result
        except DocumentError as err:
            if should_retry(err, retries):
                raise self.retry(exc=err, countdown=backoff_seconds(retries))
            raise
    finally:
        db.close()


def _index(db, document_id: str) -> None:
    """Search indexing never fails a document: it is retried by reprocessing."""
    from app.search.indexer import index_document
    try:
        index_document(db, document_id)
    except Exception:
        db.rollback()
        logger.exception(f"Search indexing failed for document {document_id}")


@celery_app.task(name="app.workers.tasks.validate_document", bind=True)
def validate_document_task(self, document_id: str):
    """Validation stage: rules + duplicates -> issues, review flags, final status."""
    from app.db.session import SessionLocal
    from app.processing.errors import DocumentError
    from app.validation.engine import validate_document

    db = SessionLocal()
    try:
        retries = self.request.retries
        try:
            result = validate_document(db, document_id, will_retry=will_retry(retries))
            _index(db, document_id)
            return result
        except DocumentError as err:
            if should_retry(err, retries):
                raise self.retry(exc=err, countdown=backoff_seconds(retries))
            raise
    finally:
        db.close()
