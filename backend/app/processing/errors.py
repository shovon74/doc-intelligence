"""
Central error codes for document processing (IMPLEMENTATION_PLAN.md, Section 9).

Every failure that is surfaced to a user carries one of these codes, a
user-friendly message, and a ``retryable`` flag. Retryable errors are transient
(network, rate limit, timeout) and may be retried with backoff; the others are
permanent for the given input and must fail immediately.
"""
from dataclasses import dataclass
from enum import Enum


class ErrorCode(str, Enum):
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    CORRUPT_FILE = "CORRUPT_FILE"
    ENCRYPTED_PDF = "ENCRYPTED_PDF"
    EMPTY_FILE = "EMPTY_FILE"
    OCR_FAILED = "OCR_FAILED"
    LLM_NOT_CONFIGURED = "LLM_NOT_CONFIGURED"
    LLM_PROVIDER_UNAVAILABLE = "LLM_PROVIDER_UNAVAILABLE"
    LLM_RATE_LIMITED = "LLM_RATE_LIMITED"
    EXTRACTION_PARSE_ERROR = "EXTRACTION_PARSE_ERROR"
    NO_TEXT_FOUND = "NO_TEXT_FOUND"
    PROCESSING_TIMEOUT = "PROCESSING_TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True)
class ErrorInfo:
    code: ErrorCode
    message: str
    retryable: bool


ERROR_INFO: dict[ErrorCode, ErrorInfo] = {
    info.code: info
    for info in (
        ErrorInfo(ErrorCode.UNSUPPORTED_TYPE,
                  "This file type is not supported. Upload a PDF, JPG, PNG, TIFF, XLSX, XLS or CSV file.",
                  False),
        ErrorInfo(ErrorCode.FILE_TOO_LARGE,
                  "The file exceeds the maximum allowed size or page/row limit.", False),
        ErrorInfo(ErrorCode.CORRUPT_FILE,
                  "The file is damaged or cannot be read.", False),
        ErrorInfo(ErrorCode.ENCRYPTED_PDF,
                  "The PDF is password protected. Remove the password and upload it again.", False),
        ErrorInfo(ErrorCode.EMPTY_FILE,
                  "The file is empty.", False),
        ErrorInfo(ErrorCode.OCR_FAILED,
                  "Text could not be recognised in the scanned document.", False),
        ErrorInfo(ErrorCode.LLM_NOT_CONFIGURED,
                  "No LLM provider is configured. Set OPENAI_API_KEY or GEMINI_API_KEY and restart.",
                  False),
        ErrorInfo(ErrorCode.LLM_PROVIDER_UNAVAILABLE,
                  "The LLM provider is temporarily unavailable. Try again shortly.", True),
        ErrorInfo(ErrorCode.LLM_RATE_LIMITED,
                  "The LLM provider rate limit was reached. The request will be retried.", True),
        ErrorInfo(ErrorCode.EXTRACTION_PARSE_ERROR,
                  "The model returned a response that could not be parsed into invoice data.",
                  False),
        ErrorInfo(ErrorCode.NO_TEXT_FOUND,
                  "No readable text was found in the document.", False),
        ErrorInfo(ErrorCode.PROCESSING_TIMEOUT,
                  "Processing took too long and was stopped. Try reprocessing the document.", True),
        ErrorInfo(ErrorCode.INTERNAL_ERROR,
                  "An unexpected error occurred while processing the document.", True),
    )
}


def get_error_info(code: ErrorCode | str) -> ErrorInfo:
    """Look up the standard message/retryable flag for a code (str or ErrorCode)."""
    return ERROR_INFO[ErrorCode(code)]


class DocumentError(Exception):
    """
    Raised anywhere in the processing path for a user-visible failure.

    ``message`` defaults to the standard text for the code; pass ``detail`` to
    append specifics (e.g. the parser's error) without losing the friendly text.
    """

    def __init__(self, code: ErrorCode | str, detail: str | None = None):
        info = get_error_info(code)
        self.code = info.code
        self.retryable = info.retryable
        self.detail = detail
        self.message = f"{info.message} ({detail})" if detail else info.message
        super().__init__(self.message)
