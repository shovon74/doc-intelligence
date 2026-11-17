from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.tasks.ping")
def ping_task():
    return {"status": "pong"}


@celery_app.task(name="app.workers.tasks.process_document", bind=True)
def process_document_task(self, document_id: str):
    """Run the processing stage (text extraction / OCR) for one uploaded document."""
    from app.db.session import SessionLocal
    from app.processing.pipeline import process_document

    db = SessionLocal()
    try:
        return process_document(
            db, document_id,
            on_progress=lambda pct: self.update_state(state="PROGRESS", meta={"progress": pct}),
        )
    finally:
        db.close()
