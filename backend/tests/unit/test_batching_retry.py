import tracemalloc
import uuid
from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.processing import pipeline
from app.processing.errors import ERROR_INFO, DocumentError, ErrorCode
from app.processing.pdf import stream_pdf_pages
from app.workers import retry, tasks
from tests.unit.test_processing import (
    LONG_TEXT, _assert_friendly_failure, _db_for, _fake_doc, make_pdf,
)


@pytest.mark.unit
def test_batch_ranges():
    assert pipeline.batch_ranges(45, 20) == [(1, 20), (21, 40), (41, 45)]
    assert pipeline.batch_ranges(20, 20) == [(1, 20)]
    assert pipeline.batch_ranges(0, 20) == []
    assert pipeline.is_batchable("pdf", 21) and not pipeline.is_batchable("pdf", 20)
    assert not pipeline.is_batchable("xlsx", 500)


@pytest.mark.unit
def test_stream_pdf_pages_range(tmp_path):
    pdf = make_pdf(tmp_path / "r.pdf", [f"{LONG_TEXT} page{i}" for i in range(1, 11)])
    pages = list(stream_pdf_pages(pdf, first_page=4, last_page=6))
    assert [p.page_number for p in pages] == [4, 5, 6]
    assert "page4" in pages[0].text
    assert [p.page_number for p in stream_pdf_pages(pdf, first_page=9, last_page=99)] == [9, 10]


@pytest.mark.unit
def test_300_page_pdf_runs_in_batches_with_bounded_memory(tmp_path):
    pdf = make_pdf(tmp_path / "big.pdf", [f"{LONG_TEXT} p{i}" for i in range(300)])
    doc = _fake_doc(pdf, "pdf")
    db = _db_for(doc)
    plan = pipeline.start_document(db, doc.id)
    assert plan.total == 300 and pipeline.is_batchable(plan.ext, plan.total)

    tracemalloc.start()
    results = [pipeline.process_pages(db, doc.id, a, b, plan.total, shared_progress=True)
               for a, b in pipeline.batch_ranges(plan.total)]
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(results) == 15 and all(r["pages"] == 20 for r in results)
    sizes = [len(c.args[0]) for c in db.add_all.call_args_list]
    assert max(sizes) <= settings.PAGE_BATCH_SIZE         # never more than one batch buffered
    pages = [p.page_number for c in db.add_all.call_args_list for p in c.args[0]]
    assert sorted(pages) == list(range(1, 301))
    assert peak < 60 * 1024 * 1024                        # does not scale with page count

    out = pipeline.finish_document(db, doc.id, results)
    assert out["pages"] == 300 and out["failed_pages"] == 0
    assert doc.status == "extracting"
    assert db.add.call_args_list[-1].args[0].details["batches"] == 15


@pytest.mark.unit
def test_failed_batch_does_not_fail_document_unless_all_fail(tmp_path):
    pdf = make_pdf(tmp_path / "p.pdf", [LONG_TEXT] * 40)
    doc = _fake_doc(pdf, "pdf")
    db = _db_for(doc)
    plan = pipeline.start_document(db, doc.id)
    good = pipeline.process_pages(db, doc.id, 1, 20, plan.total, shared_progress=True)
    bad = pipeline.failed_batch_result(21, 40)
    out = pipeline.finish_document(db, doc.id, [good, bad])
    assert doc.status == "extracting" and out["failed_pages"] == 20

    doc2 = _fake_doc(pdf, "pdf")
    db2 = _db_for(doc2)
    pipeline.start_document(db2, doc2.id)
    with pytest.raises(DocumentError) as ei:
        pipeline.finish_document(db2, doc2.id, [pipeline.failed_batch_result(1, 20),
                                                pipeline.failed_batch_result(21, 40)])
    assert ei.value.code is ErrorCode.OCR_FAILED
    _assert_friendly_failure(doc2, ErrorCode.OCR_FAILED)


@pytest.mark.unit
def test_lost_batch_counts_as_failed(tmp_path):
    pdf = make_pdf(tmp_path / "p.pdf", [LONG_TEXT] * 40)
    doc = _fake_doc(pdf, "pdf")
    db = _db_for(doc)
    plan = pipeline.start_document(db, doc.id)
    good = pipeline.process_pages(db, doc.id, 1, 20, plan.total, shared_progress=True)
    out = pipeline.finish_document(db, doc.id, [good])      # second batch never reported
    assert out["failed_pages"] == 20 and doc.status == "extracting"


# ---------------------------------------------------------------- retry policy
@pytest.mark.unit
@pytest.mark.parametrize("code", list(ErrorCode))
def test_only_retryable_codes_retry(code):
    err = DocumentError(code)
    assert retry.should_retry(err, 0) is ERROR_INFO[code].retryable
    assert not retry.should_retry(err, settings.TASK_MAX_RETRIES)      # retries exhausted


@pytest.mark.unit
def test_backoff_doubles_and_is_capped():
    vals = [retry.backoff_seconds(n, jitter=False) for n in range(10)]
    base = settings.TASK_RETRY_BACKOFF_SECONDS
    assert vals[:3] == [base, base * 2, base * 4] and max(vals) == retry.MAX_BACKOFF_SECONDS
    assert all(0.8 * base <= retry.backoff_seconds(0) <= 1.2 * base + 1 for _ in range(20))


@pytest.mark.unit
def test_retryable_failure_keeps_document_processing_when_retry_pending(tmp_path, monkeypatch):
    f = tmp_path / "a.csv"
    f.write_text("a,b\n1,2\n")
    doc = _fake_doc(f, "csv")
    db = _db_for(doc)
    monkeypatch.setattr("app.processing.pipeline._count_pages",
                        MagicMock(side_effect=ValueError("flaky")))    # INTERNAL_ERROR is retryable
    with pytest.raises(DocumentError) as ei:
        pipeline.process_document(db, doc.id, will_retry=True)
    assert ei.value.retryable
    assert doc.status == "processing" and doc.error_code is None
    assert db.add.call_args_list[-1].args[0].status == "retried"

    with pytest.raises(DocumentError):                                 # last attempt is final
        pipeline.process_document(db, doc.id, will_retry=False)
    _assert_friendly_failure(doc, ErrorCode.INTERNAL_ERROR)


@pytest.mark.unit
def test_permanent_failure_is_final_even_when_retries_remain(tmp_path):
    f = tmp_path / "empty.csv"
    f.write_bytes(b"")
    doc = _fake_doc(f, "csv")
    db = _db_for(doc)
    with pytest.raises(DocumentError):
        pipeline.process_document(db, doc.id, will_retry=True)
    _assert_friendly_failure(doc, ErrorCode.EMPTY_FILE)
    assert db.add.call_args_list[-1].args[0].status == "failed"


# ---------------------------------------------------------------- celery tasks
class _Retried(Exception):
    pass


def _task_env(monkeypatch, start):
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr("app.processing.pipeline.start_document", start)
    retry_mock = MagicMock(side_effect=_Retried)
    monkeypatch.setattr(tasks.process_document_task, "retry", retry_mock)
    return retry_mock


@pytest.mark.unit
def test_task_retries_retryable_error_with_backoff(monkeypatch):
    retry_mock = _task_env(
        monkeypatch, MagicMock(side_effect=DocumentError(ErrorCode.LLM_RATE_LIMITED)))
    with pytest.raises(_Retried):
        tasks.process_document_task.run(str(uuid.uuid4()))
    assert retry_mock.call_args.kwargs["countdown"] >= 1


@pytest.mark.unit
def test_task_does_not_retry_permanent_error(monkeypatch):
    retry_mock = _task_env(
        monkeypatch, MagicMock(side_effect=DocumentError(ErrorCode.CORRUPT_FILE)))
    with pytest.raises(DocumentError) as ei:
        tasks.process_document_task.run(str(uuid.uuid4()))
    assert ei.value.code is ErrorCode.CORRUPT_FILE
    retry_mock.assert_not_called()


@pytest.mark.unit
def test_task_dispatches_large_pdf_as_chord_of_batches(monkeypatch):
    _task_env(monkeypatch, MagicMock(return_value=pipeline.Plan(total=45, ext="pdf")))
    captured = {}
    monkeypatch.setattr(tasks, "chord",
                        lambda header: lambda cb: captured.update(header=header, cb=cb))
    doc_id = str(uuid.uuid4())
    out = tasks.process_document_task.run(doc_id)
    assert out["batches"] == 3 and out["dispatched"]
    ranges = [(s.args[1], s.args[2]) for s in captured["header"].tasks]
    assert ranges == [(1, 20), (21, 40), (41, 45)]
    assert captured["cb"].args == (doc_id,)
    assert captured["cb"].task == "app.workers.tasks.finish_document"


@pytest.mark.unit
def test_batch_task_never_raises(monkeypatch):
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr("app.processing.pipeline.process_pages",
                        MagicMock(side_effect=RuntimeError("db gone")))
    res = tasks.process_pages_task.run(str(uuid.uuid4()), 21, 40, 100)
    assert res["failed"] == 20 and res["pages"] == 20
