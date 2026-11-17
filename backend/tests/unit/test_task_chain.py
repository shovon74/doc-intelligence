import uuid
from unittest.mock import MagicMock

import pytest

from app.processing import pipeline
from app.processing.errors import DocumentError, ErrorCode
from app.workers import tasks


class _Retried(Exception):
    pass


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: MagicMock())
    delay = MagicMock()
    monkeypatch.setattr(tasks.extract_document_task, "delay", delay)
    return delay


@pytest.mark.unit
def test_small_document_chains_to_extraction(env, monkeypatch):
    monkeypatch.setattr("app.processing.pipeline.start_document",
                        MagicMock(return_value=pipeline.Plan(total=3, ext="pdf")))
    monkeypatch.setattr("app.processing.pipeline.process_planned",
                        MagicMock(return_value={"pages": 3}))
    doc_id = str(uuid.uuid4())
    assert tasks.process_document_task.run(doc_id) == {"pages": 3}
    env.assert_called_once_with(doc_id)


@pytest.mark.unit
def test_failed_processing_does_not_chain(env, monkeypatch):
    monkeypatch.setattr("app.processing.pipeline.start_document",
                        MagicMock(side_effect=DocumentError(ErrorCode.CORRUPT_FILE)))
    with pytest.raises(DocumentError):
        tasks.process_document_task.run(str(uuid.uuid4()))
    env.assert_not_called()


@pytest.mark.unit
def test_batch_join_chains_to_extraction_only_on_success(env, monkeypatch):
    doc_id = str(uuid.uuid4())
    monkeypatch.setattr("app.processing.pipeline.finish_document",
                        MagicMock(return_value={"pages": 40}))
    assert tasks.finish_document_task.run([{"pages": 40}], doc_id) == {"pages": 40}
    env.assert_called_once_with(doc_id)

    env.reset_mock()
    monkeypatch.setattr("app.processing.pipeline.finish_document",
                        MagicMock(side_effect=DocumentError(ErrorCode.NO_TEXT_FOUND)))
    out = tasks.finish_document_task.run([], doc_id)
    assert out["failed"] and out["code"] == "NO_TEXT_FOUND"
    env.assert_not_called()


@pytest.mark.unit
def test_extraction_task_retries_only_retryable_errors(monkeypatch):
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: MagicMock())
    retry = MagicMock(side_effect=_Retried)
    monkeypatch.setattr(tasks.extract_document_task, "retry", retry)

    monkeypatch.setattr("app.extraction.stage.extract_document",
                        MagicMock(side_effect=DocumentError(ErrorCode.LLM_RATE_LIMITED)))
    with pytest.raises(_Retried):
        tasks.extract_document_task.run(str(uuid.uuid4()))
    assert retry.call_args.kwargs["countdown"] >= 1

    retry.reset_mock()
    monkeypatch.setattr("app.extraction.stage.extract_document",
                        MagicMock(side_effect=DocumentError(ErrorCode.LLM_NOT_CONFIGURED)))
    with pytest.raises(DocumentError):
        tasks.extract_document_task.run(str(uuid.uuid4()))
    retry.assert_not_called()
