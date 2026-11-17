import pytest

from app.processing.errors import (
    ERROR_INFO,
    DocumentError,
    ErrorCode,
    get_error_info,
)

# Section 9 of IMPLEMENTATION_PLAN.md, verbatim.
SECTION_9_CODES = {
    "UNSUPPORTED_TYPE", "FILE_TOO_LARGE", "CORRUPT_FILE", "ENCRYPTED_PDF", "EMPTY_FILE",
    "OCR_FAILED", "LLM_NOT_CONFIGURED", "LLM_PROVIDER_UNAVAILABLE", "LLM_RATE_LIMITED",
    "EXTRACTION_PARSE_ERROR", "NO_TEXT_FOUND", "PROCESSING_TIMEOUT", "INTERNAL_ERROR",
}


@pytest.mark.unit
def test_codes_match_plan_section_9():
    assert {c.value for c in ErrorCode} == SECTION_9_CODES


@pytest.mark.unit
@pytest.mark.parametrize("code", list(ErrorCode))
def test_every_code_has_message_and_retryable_flag(code):
    info = ERROR_INFO[code]
    assert info.code == code
    assert info.message.strip()
    assert isinstance(info.retryable, bool)


@pytest.mark.unit
def test_only_transient_errors_are_retryable():
    retryable = {c.value for c, i in ERROR_INFO.items() if i.retryable}
    assert retryable == {
        "LLM_PROVIDER_UNAVAILABLE", "LLM_RATE_LIMITED", "PROCESSING_TIMEOUT", "INTERNAL_ERROR",
    }


@pytest.mark.unit
def test_lookup_accepts_string_and_rejects_unknown():
    assert get_error_info("CORRUPT_FILE").code is ErrorCode.CORRUPT_FILE
    with pytest.raises(ValueError):
        get_error_info("NOT_A_CODE")


@pytest.mark.unit
def test_document_error_carries_code_message_and_detail():
    err = DocumentError(ErrorCode.CORRUPT_FILE, "xref table missing")
    assert err.code is ErrorCode.CORRUPT_FILE and err.retryable is False
    assert "damaged" in err.message and "xref table missing" in err.message
    assert str(err) == err.message

    plain = DocumentError("LLM_RATE_LIMITED")
    assert plain.retryable is True and plain.detail is None
