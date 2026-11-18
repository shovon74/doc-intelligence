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
                  "This file type is not supported. Upload a PDF, JPG, PNG, TIFF, XLSX, XLS or "
                  "CSV file.", False),
        ErrorInfo(ErrorCode.FILE_TOO_LARGE,
                  "The file is too large to process. Split it into smaller files and upload "
                  "them again.", False),
        ErrorInfo(ErrorCode.CORRUPT_FILE,
                  "The file is damaged or cannot be read. Check that it opens on your computer, "
                  "then upload it again.", False),
        ErrorInfo(ErrorCode.ENCRYPTED_PDF,
                  "The PDF is password protected. Remove the password and upload it again.",
                  False),
        ErrorInfo(ErrorCode.EMPTY_FILE,
                  "The file is empty. Upload a file that contains data.", False),
        ErrorInfo(ErrorCode.OCR_FAILED,
                  "The scanned text could not be read. Upload a clearer, higher-resolution scan "
                  "or a text-based file, or try Reprocess.", False),
        ErrorInfo(ErrorCode.LLM_NOT_CONFIGURED,
                  "No AI provider is configured. Ask an administrator to set OPENAI_API_KEY or "
                  "GEMINI_API_KEY, then use Reprocess.", False),
        ErrorInfo(ErrorCode.LLM_PROVIDER_UNAVAILABLE,
                  "The AI provider is temporarily unavailable. Try Reprocess in a few minutes.",
                  True),
        ErrorInfo(ErrorCode.LLM_RATE_LIMITED,
                  "The AI provider is busy (rate limit reached). It will be retried "
                  "automatically; you can also try Reprocess later.", True),
        ErrorInfo(ErrorCode.EXTRACTION_PARSE_ERROR,
                  "The AI response could not be understood. Try Reprocess, choose another "
                  "provider if one is available, or enter the values manually.", False),
        ErrorInfo(ErrorCode.NO_TEXT_FOUND,
                  "No readable text was found in the document. Upload a clearer scan or a "
                  "text-based file.", False),
        ErrorInfo(ErrorCode.PROCESSING_TIMEOUT,
                  "Processing took too long and was stopped. Try Reprocess; for very large "
                  "files, split them first.", True),
        ErrorInfo(ErrorCode.INTERNAL_ERROR,
                  "Something went wrong while processing the document. Try Reprocess; if it "
                  "keeps failing, contact support.", True),
    )
}


def get_error_info(code: ErrorCode | str) -> ErrorInfo:
    """Look up the standard message/retryable flag for a code (str or ErrorCode)."""
    return ERROR_INFO[ErrorCode(code)]


class DocumentError(Exception):
    """
    Raised anywhere in the processing path for a user-visible failure.

    ``message`` is ALWAYS the standard user-facing text for the code. ``detail``
    holds technical information (parser error, path, ...) for logs and the
    processing-event details only; it must never be shown to end users.
    """

    def __init__(self, code: ErrorCode | str, detail: str | None = None):
        info = get_error_info(code)
        self.code = info.code
        self.retryable = info.retryable
        self.message = info.message
        self.detail = detail
        super().__init__(f"{info.code.value}: {detail}" if detail else info.code.value)

    def __reduce__(self):
        return (DocumentError, (self.code, self.detail))     # picklable, so Celery can store it


def to_document_error(exc: BaseException) -> DocumentError:
    """Convert any exception into a DocumentError; unknown ones become INTERNAL_ERROR."""
    if isinstance(exc, DocumentError):
        return exc
    if isinstance(exc, TimeoutError):
        return DocumentError(ErrorCode.PROCESSING_TIMEOUT, repr(exc))
    return DocumentError(ErrorCode.INTERNAL_ERROR, repr(exc))
