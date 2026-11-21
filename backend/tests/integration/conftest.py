import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import settings


@pytest.fixture(scope="session")
def pg_engine():
    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            conn.execute(text("SELECT 1 FROM users LIMIT 1"))   # migrations applied?
    except Exception as e:                                       # pragma: no cover
        pytest.skip(f"Postgres with migrated schema not available: {e}")
    yield engine
    engine.dispose()


@pytest.fixture
def db(pg_engine):
    """A session whose work is rolled back at the end: tests never leave rows behind."""
    conn = pg_engine.connect()
    outer = conn.begin()
    session = Session(bind=conn, autoflush=False, join_transaction_mode="create_savepoint")   # as in production
    yield session
    session.close()
    outer.rollback()
    conn.close()
