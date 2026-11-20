import pytest

from app.core import runtime_config
from app.core.config import settings


@pytest.fixture(autouse=True)
def _cfg_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    runtime_config.clear()
    yield
    runtime_config.clear()


@pytest.mark.unit
def test_ui_config_overrides_env_and_persists(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    assert settings.has_gemini is False
    runtime_config.save("gemini", "gemini-2.5-pro", "AIza-test")
    assert settings.has_gemini and settings.GEMINI_API_KEY == "AIza-test"
    assert settings.GEMINI_MODEL == "gemini-2.5-pro"
    assert settings.LLM_PROVIDER == "gemini" and settings.EMBEDDING_PROVIDER == "gemini"


@pytest.mark.unit
def test_switch_provider_keeps_other_key():
    runtime_config.save("gemini", "g", "gk")
    runtime_config.save("openai", "gpt-4o-mini", "ok")
    assert settings.LLM_PROVIDER == "openai" and settings.OPENAI_API_KEY == "ok"
    runtime_config.save("gemini", "g", None)             # keep stored key
    assert settings.GEMINI_API_KEY == "gk"
