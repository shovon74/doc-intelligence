"""Retry policy: retry with exponential backoff only for retryable error codes."""
import random

from app.core.config import settings
from app.processing.errors import DocumentError

MAX_BACKOFF_SECONDS = 300


def backoff_seconds(retries: int, jitter: bool = True) -> int:
    """Delay before retry number ``retries`` (0-based): base * 2**retries, capped, +/-20% jitter."""
    delay = min(settings.TASK_RETRY_BACKOFF_SECONDS * (2 ** retries), MAX_BACKOFF_SECONDS)
    if jitter:
        delay = int(delay * random.uniform(0.8, 1.2))
    return max(delay, 1)


def will_retry(retries_so_far: int) -> bool:
    """True while retries remain; the pipeline then keeps a retryable failure non-final."""
    return retries_so_far < settings.TASK_MAX_RETRIES


def should_retry(err: DocumentError, retries_so_far: int) -> bool:
    """Retry only transient errors, and only while retries remain. Permanent errors never retry."""
    return err.retryable and will_retry(retries_so_far)
