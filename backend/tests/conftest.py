import pytest
from typing import Generator
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from app.main import app
from app.db.session import get_db


@pytest.fixture
def mock_db():
    db = MagicMock()
    return db


@pytest.fixture
def client(mock_db) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_db] = lambda: mock_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _isolated_ui_config(tmp_path, monkeypatch):
    """A key saved through the UI must never leak into tests (it overrides settings)."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "ui-config"))
