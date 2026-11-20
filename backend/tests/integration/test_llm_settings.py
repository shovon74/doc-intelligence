import uuid
import pytest

from app.core import runtime_config
from app.core.config import settings
from tests.integration.test_api import API, client, signup  # noqa: F401  (client fixture)


@pytest.fixture(autouse=True)
def _cfg_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    runtime_config.clear()
    yield
    runtime_config.clear()


@pytest.mark.integration
def test_settings_endpoint_never_returns_key(client, monkeypatch):
    from app.api.v1 import llm_settings
    monkeypatch.setattr(llm_settings, "_check_key", lambda *a: None)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    h = signup(client, f"llmset{uuid.uuid4().hex[:8]}@example.com")
    assert client.get(f"{API}/settings/llm").status_code == 401
    assert client.post(f"{API}/settings/llm", headers=h,
                       json={"provider": "gemini", "model": "m"}).status_code == 422
    r = client.post(f"{API}/settings/llm", headers=h,
                    json={"provider": "gemini", "model": "m", "api_key": "SECRET-KEY"})
    assert r.status_code == 200 and r.json()["configured"] and "SECRET-KEY" not in r.text
    assert "SECRET-KEY" not in client.get(f"{API}/settings/llm", headers=h).text
