"""Shared API dependencies."""
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status


@dataclass
class CurrentUser:
    id: uuid.UUID


def get_current_user() -> CurrentUser:
    """Placeholder until the auth work package: every request is rejected."""
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication is not configured.")
