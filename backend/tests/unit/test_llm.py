import json
from unittest.mock import MagicMock

import pytest
from tenacity import wait_none

from app.core.config import settings
from app.llm import factory
from app.llm.base import LLMProvider, parse_json_object
from app.llm.fake import FakeProvider
from app.processing.errors import DocumentError, ErrorCode


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(LLMProvider, "retry_wait", wait_none())


def _keys(monkeypatch, openai=None, gemini=None, provider="auto", preference="openai,gemini",
          fallback=True):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", openai)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", gemini)
    monkeypatch.setattr(settings, "LLM_PROVIDER", provider)
    monkeypatch.setattr(settings, "LLM_PREFERENCE", preference)
    monkeypatch.setattr(settings, "LLM_FALLBACK_ENABLED", fallback)


# ---------------------------------------------------------------- factory
@pytest.mark.unit
def test_factory_only_openai(monkeypatch):
    _keys(monkeypatch, openai="sk-x")
    r = factory.resolve_llm()
    assert r.primary.name == "openai" and r.fallback is None


@pytest.mark.unit
def test_factory_only_gemini(monkeypatch):
    _keys(monkeypatch, gemini="g-x")
    r = factory.resolve_llm()
    assert r.primary.name == "gemini" and r.fallback is None


@pytest.mark.unit
def test_factory_both_follows_preference_and_sets_fallback(monkeypatch):
    _keys(monkeypatch, openai="sk-x", gemini="g-x")
    r = factory.resolve_llm()
    assert (r.primary.name, r.fallback.name) == ("openai", "gemini")
    _keys(monkeypatch, openai="sk-x", gemini="g-x", preference="gemini,openai")
    r = factory.resolve_llm()
    assert (r.primary.name, r.fallback.name) == ("gemini", "openai")


@pytest.mark.unit
def test_factory_fallback_can_be_disabled(monkeypatch):
    _keys(monkeypatch, openai="sk-x", gemini="g-x", fallback=False)
    assert factory.resolve_llm().fallback is None


@pytest.mark.unit
def test_factory_none_configured(monkeypatch):
    _keys(monkeypatch)
    with pytest.raises(DocumentError) as ei:
        factory.resolve_llm()
    assert ei.value.code is ErrorCode.LLM_NOT_CONFIGURED
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "auto")
    assert factory.resolve_embedder() is None                   # auto: no key -> no embedder
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "gemini")
    with pytest.raises(DocumentError):                          # explicit: key required
        factory.resolve_embedder()


@pytest.mark.unit
def test_factory_explicit_but_missing_key(monkeypatch):
    _keys(monkeypatch, openai="sk-x", provider="gemini")
    with pytest.raises(DocumentError) as ei:
        factory.resolve_llm()
    assert ei.value.code is ErrorCode.LLM_NOT_CONFIGURED
    with pytest.raises(DocumentError):
        factory.resolve_llm("gemini")            # per-document request is explicit too


@pytest.mark.unit
def test_factory_explicit_choice_has_no_fallback_and_request_overrides_setting(monkeypatch):
    _keys(monkeypatch, openai="sk-x", gemini="g-x", provider="openai")
    r = factory.resolve_llm()
    assert r.primary.name == "openai" and r.fallback is None
    assert factory.resolve_llm("gemini").primary.name == "gemini"
    assert factory.resolve_llm("auto").primary.name == "openai"    # auto request: preference order


@pytest.mark.unit
def test_embedder_resolution(monkeypatch):
    _keys(monkeypatch, gemini="g-x")
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "auto")
    assert factory.resolve_embedder().name == "gemini"
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "openai")
    with pytest.raises(DocumentError):
        factory.resolve_embedder()


# ---------------------------------------------------------------- fake provider
@pytest.mark.unit
def test_fake_returns_null_for_absent_fields_and_never_invents():
    p = FakeProvider()
    res = p.complete_json("sys", "Invoice Number: A-1\nTotal: 10.00\nthanks for your business")
    d = res.data
    assert d["invoice_number"] == "A-1" and d["total"] == "10.00"
    for absent in ("invoice_date", "supplier_name", "customer_name", "subtotal", "tax",
                   "currency"):
        assert d[absent] is None
    assert d["line_items"] == [] and set(d["field_evidence"]) == {"invoice_number", "total"}


@pytest.mark.unit
def test_fake_embeddings_are_deterministic_768_dim_and_unit_length():
    p = FakeProvider()
    a, b = p.embed(["acme invoice", "acme invoice"])
    assert a == b and len(a) == settings.EMBEDDING_DIM
    assert sum(x * x for x in a) == pytest.approx(1.0)
    assert p.embed(["completely different words"])[0] != a


# ---------------------------------------------------------------- shared provider logic
class _Scripted(LLMProvider):
    name, model, embedding_model = "scripted", "s-1", "s-e"

    def __init__(self, script):
        self.script, self.calls, self.users = list(script), 0, []

    def _complete(self, system, user):
        self.calls += 1
        self.users.append(user)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, 10, 5

    def embed(self, texts):
        return []


@pytest.mark.unit
def test_json_fences_are_tolerated():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]")


@pytest.mark.unit
def test_one_repair_attempt_then_success():
    p = _Scripted(["not json at all", json.dumps({"ok": True})])
    res = p.complete_json("s", "u")
    assert res.data == {"ok": True} and p.calls == 2
    assert "previous reply was invalid" in p.users[1]
    assert res.tokens_in == 20 and res.tokens_out == 10      # both calls are counted


@pytest.mark.unit
def test_repair_is_attempted_once_only():
    p = _Scripted(["bad", "still bad", json.dumps({"never": "reached"})])
    with pytest.raises(DocumentError) as ei:
        p.complete_json("s", "u")
    assert ei.value.code is ErrorCode.EXTRACTION_PARSE_ERROR and p.calls == 2


@pytest.mark.unit
def test_validation_failure_triggers_repair():
    def validate(d):
        if "total" not in d:
            raise ValueError("missing total")

    p = _Scripted([json.dumps({}), json.dumps({"total": "1"})])
    assert p.complete_json("s", "u", validate).data == {"total": "1"} and p.calls == 2


@pytest.mark.unit
def test_transient_errors_are_retried_then_succeed(monkeypatch):
    monkeypatch.setattr(settings, "LLM_MAX_RETRIES", 3)
    p = _Scripted([DocumentError(ErrorCode.LLM_RATE_LIMITED),
                   DocumentError(ErrorCode.LLM_PROVIDER_UNAVAILABLE), json.dumps({"x": 1})])
    assert p.complete_json("s", "u").data == {"x": 1} and p.calls == 3


@pytest.mark.unit
def test_transient_errors_give_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(settings, "LLM_MAX_RETRIES", 3)
    p = _Scripted([DocumentError(ErrorCode.LLM_RATE_LIMITED)] * 5)
    with pytest.raises(DocumentError) as ei:
        p.complete_json("s", "u")
    assert ei.value.code is ErrorCode.LLM_RATE_LIMITED and p.calls == 3


@pytest.mark.unit
def test_permanent_errors_are_not_retried():
    p = _Scripted([DocumentError(ErrorCode.LLM_NOT_CONFIGURED), json.dumps({})])
    with pytest.raises(DocumentError):
        p.complete_json("s", "u")
    assert p.calls == 1


# ---------------------------------------------------------------- adapters (SDK mocked)
@pytest.mark.unit
def test_openai_adapter_request_shape_and_error_mapping(monkeypatch):
    import openai
    from app.llm.openai_provider import OpenAIProvider, classify_error

    p = OpenAIProvider("sk-test")
    create = MagicMock(return_value=MagicMock(
        choices=[MagicMock(message=MagicMock(content='{"a": 1}'))],
        usage=MagicMock(prompt_tokens=7, completion_tokens=3)))
    p.client = MagicMock()
    p.client.chat.completions.create = create
    res = p.complete_json("sys", "usr")
    kw = create.call_args.kwargs
    assert kw["temperature"] == 0 and kw["response_format"] == {"type": "json_object"}
    assert res.data == {"a": 1} and (res.tokens_in, res.tokens_out) == (7, 3)
    assert res.provider == "openai"

    req = MagicMock()
    resp = MagicMock(status_code=429, headers={})
    assert classify_error(openai.RateLimitError("x", response=resp, body=None)).code \
        is ErrorCode.LLM_RATE_LIMITED
    assert classify_error(openai.APIConnectionError(request=req)).code \
        is ErrorCode.LLM_PROVIDER_UNAVAILABLE
    resp401 = MagicMock(status_code=401, headers={})
    assert classify_error(openai.AuthenticationError("x", response=resp401, body=None)).code \
        is ErrorCode.LLM_NOT_CONFIGURED


@pytest.mark.unit
def test_openai_embeddings_request_768_dims():
    from app.llm.openai_provider import OpenAIProvider

    p = OpenAIProvider("sk-test")
    p.client = MagicMock()
    p.client.embeddings.create.return_value = MagicMock(data=[
        MagicMock(index=1, embedding=[2.0]), MagicMock(index=0, embedding=[1.0])])
    assert p.embed(["a", "b"]) == [[1.0], [2.0]]
    assert p.client.embeddings.create.call_args.kwargs["dimensions"] == settings.EMBEDDING_DIM


@pytest.mark.unit
def test_gemini_adapter_request_shape_and_error_mapping():
    from app.llm.gemini_provider import GeminiProvider, classify_error

    p = GeminiProvider("g-test")
    p.client = MagicMock()
    p.client.models.generate_content.return_value = MagicMock(
        text='{"b": 2}', usage_metadata=MagicMock(prompt_token_count=4,
                                                  candidates_token_count=2))
    res = p.complete_json("sys", "usr")
    cfg = p.client.models.generate_content.call_args.kwargs["config"]
    assert cfg.temperature == 0 and cfg.response_mime_type == "application/json"
    assert res.data == {"b": 2} and (res.tokens_in, res.tokens_out) == (4, 2)

    class Err(Exception):
        def __init__(self, code):
            self.code = code

    assert classify_error(Err(429)).code is ErrorCode.LLM_RATE_LIMITED
    assert classify_error(Err(503)).code is ErrorCode.LLM_PROVIDER_UNAVAILABLE
    assert classify_error(Err(403)).code is ErrorCode.LLM_NOT_CONFIGURED
    assert classify_error(ConnectionError("down")).code is ErrorCode.LLM_PROVIDER_UNAVAILABLE


@pytest.mark.unit
def test_gemini_embeddings_request_768_dims():
    from app.llm.gemini_provider import GeminiProvider

    p = GeminiProvider("g-test")
    p.client = MagicMock()
    p.client.models.embed_content.return_value = MagicMock(
        embeddings=[MagicMock(values=[0.5, 0.5])])
    assert p.embed(["a"]) == [[0.5, 0.5]]
    cfg = p.client.models.embed_content.call_args.kwargs["config"]
    assert cfg.output_dimensionality == settings.EMBEDDING_DIM
