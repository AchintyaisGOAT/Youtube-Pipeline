"""app/llm.py routing (README §5) and app/quota.py daily caps. Providers are faked at the
`_call` / SDK-client boundary — no network, no keys."""

from __future__ import annotations

import types

import pytest

from app import llm, quota
from app.config import get_config
from app.db import Base, LlmCache, LlmCall, get_engine, get_sessionmaker
from app.quota import QuotaExceeded, RetryLater


@pytest.fixture
def session(tmp_path):
    url = f"sqlite:///{tmp_path / 'llm.db'}"
    Base.metadata.create_all(get_engine(url))
    with get_sessionmaker(url)() as s:
        yield s


@pytest.fixture
def fake_call(monkeypatch):
    """Scripted per-model behaviour: a str is returned as the response text, an
    exception instance is raised. Records every model actually called."""
    behaviour: dict[str, object] = {}
    calls: list[str] = []

    def _call(model, prompt, json_mode):
        calls.append(model)
        result = behaviour[model]
        if isinstance(result, Exception):
            raise result
        return result, 10, 5

    monkeypatch.setattr(llm, "_call", _call)
    return behaviour, calls


def _outcomes(session) -> list[tuple[str, str]]:
    session.flush()
    return [(c.model, c.outcome) for c in session.query(LlmCall).order_by(LlmCall.created_at)]


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #
def test_roles_resolve_to_configured_models_and_providers():
    cfg = get_config().llm
    assert llm.models_for("writer") == [cfg.writer, cfg.writer_fallback]
    assert llm.models_for("checker") == [cfg.checker]  # no fallback: must differ from writer
    assert llm.models_for("worker") == [cfg.worker, cfg.worker_fallback]
    assert llm.provider_of("gemini-3.8-flash") == "gemini"
    assert llm.provider_of("openai/gpt-oss-120b") == "groq"


def test_primary_success_is_logged_and_cached(session, fake_call):
    behaviour, calls = fake_call
    worker = llm.models_for("worker")[0]
    behaviour[worker] = '{"verdict": "pass"}'

    first = llm.generate(session, "p", role="worker", step="gate", inputs={"id": 1})
    second = llm.generate(session, "p", role="worker", step="gate", inputs={"id": 1})

    assert first == second == {"verdict": "pass"}
    assert calls == [worker]  # second answer came from llm_cache
    assert _outcomes(session) == [(worker, "ok")]
    assert session.query(LlmCache).one().model == worker


def test_rate_limited_primary_falls_back(session, fake_call):
    behaviour, calls = fake_call
    primary, fallback = llm.models_for("writer")
    behaviour[primary] = RetryLater("429")
    behaviour[fallback] = "the script"

    assert llm.generate(session, "p", role="writer", step="script", json_mode=False) == "the script"
    assert _outcomes(session) == [(primary, "retry_later"), (fallback, "ok")]


def test_bad_json_from_primary_falls_back(session, fake_call):
    behaviour, _ = fake_call
    primary, fallback = llm.models_for("worker")
    behaviour[primary] = "not json at all"
    behaviour[fallback] = '```json\n{"verdict": "veto"}\n```'  # fenced JSON is tolerated

    assert llm.generate(session, "p", role="worker", step="gate") == {"verdict": "veto"}
    assert _outcomes(session) == [(primary, "error"), (fallback, "ok")]


def test_waits_rather_than_fails_when_any_model_was_only_unavailable(session, fake_call):
    behaviour, _ = fake_call
    primary, fallback = llm.models_for("worker")
    behaviour[primary] = ValueError("404 model not found")
    behaviour[fallback] = RetryLater("429")

    with pytest.raises(RetryLater):
        llm.generate(session, "p", role="worker", step="gate")


def test_fails_when_every_model_really_failed(session, fake_call):
    behaviour, _ = fake_call
    for model in llm.models_for("worker"):
        behaviour[model] = ValueError(f"{model} broke")

    with pytest.raises(ValueError):
        llm.generate(session, "p", role="worker", step="gate")


def test_local_daily_cap_skips_to_fallback_without_counting(session, fake_call, monkeypatch):
    behaviour, calls = fake_call
    primary, fallback = llm.models_for("worker")  # groq primary, gemini fallback
    behaviour[fallback] = '{"verdict": "pass"}'
    monkeypatch.setitem(quota.DAILY_LIMITS, "groq", {"requests": 0})

    assert llm.generate(session, "p", role="worker", step="gate") == {"verdict": "pass"}
    assert calls == [fallback]
    assert _outcomes(session) == [(primary, "over_budget"), (fallback, "ok")]
    assert quota.usage_today(session, "groq") == (0, 0)  # over_budget never reached Groq


def test_quota_counts_requests_and_tokens(session, monkeypatch):
    assert issubclass(QuotaExceeded, RetryLater)
    monkeypatch.setitem(quota.DAILY_LIMITS, "groq", {"requests": 2, "tokens": 1_000})
    for outcome in ("ok", "error"):
        session.add(LlmCall(provider="groq", model="m", step="gate", outcome=outcome,
                            prompt_tokens=100, response_tokens=50))
    session.flush()

    assert quota.usage_today(session, "groq") == (2, 300)
    with pytest.raises(QuotaExceeded):
        quota.check(session, "groq")


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #
def test_groq_request_uses_json_mode_and_low_reasoning(monkeypatch):
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        message = types.SimpleNamespace(content='{"ok": true}')
        usage = types.SimpleNamespace(prompt_tokens=7, completion_tokens=3)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=usage)

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "_groq", lambda: client)

    assert llm._call_groq("openai/gpt-oss-120b", "p", True) == ('{"ok": true}', 7, 3)
    assert seen["response_format"] == {"type": "json_object"}
    assert seen["reasoning_effort"] == "low"


def test_groq_rate_limit_becomes_retry_later(monkeypatch):
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    error = openai.RateLimitError("slow down", response=httpx.Response(429, request=request), body=None)

    def create(**kwargs):
        raise error

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "_groq", lambda: client)

    with pytest.raises(RetryLater):
        llm._call_groq("openai/gpt-oss-120b", "p", True)


def test_gemini_exhausted_rate_limit_becomes_retry_later(monkeypatch):
    import tenacity
    from google.genai import errors as genai_errors

    class FakeModels:
        def generate_content(self, **kwargs):
            raise genai_errors.ClientError(429, {"error": {"message": "RESOURCE_EXHAUSTED"}})

    monkeypatch.setattr(llm, "_gemini", lambda: types.SimpleNamespace(models=FakeModels()))
    monkeypatch.setattr(tenacity, "wait_exponential", lambda **kwargs: tenacity.wait_none())

    with pytest.raises(RetryLater):
        llm._generate_with_retry("gemini-3.8-flash", "prompt", None)


def test_gemini_dropped_connection_is_transient():
    import httpx
    from google.genai import errors as genai_errors

    assert llm._is_transient(httpx.RemoteProtocolError("Server disconnected without sending a response."))
    assert llm._is_transient(httpx.ConnectTimeout("timed out"))
    assert not llm._is_transient(genai_errors.ClientError(404, {"error": {"message": "not found"}}))



# --------------------------------------------------------------------------- #
# bundle A: refusals and daily caps never waste calls or fail a video
# --------------------------------------------------------------------------- #
def _gemini_raising(monkeypatch, error):
    calls = []

    class FakeModels:
        def generate_content(self, **kwargs):
            calls.append(kwargs["model"])
            raise error

    monkeypatch.setattr(llm, "_gemini", lambda: types.SimpleNamespace(models=FakeModels()))
    monkeypatch.setattr(llm.notify, "alert", lambda message: None)
    return calls


def test_gemini_402_blocks_gemini_for_the_rest_of_the_run(session, monkeypatch):
    from google.genai import errors as genai_errors

    calls = _gemini_raising(monkeypatch, genai_errors.ClientError(
        402, {"error": {"message": "Your prepayment credits are depleted."}}))
    with pytest.raises(quota.ProviderBlocked):
        llm._generate_with_retry("gemini-3.8-flash", "p", None)
    assert calls == ["gemini-3.8-flash"]  # not retried
    with pytest.raises(quota.ProviderBlocked):  # every later Gemini call is skipped without a request
        quota.check(session, "gemini", "gemini-3.5-flash")
    quota.check(session, "groq", "openai/gpt-oss-120b")  # Groq is unaffected


def test_a_blocked_checker_makes_the_video_wait_not_fail(session, fake_call):
    behaviour, _ = fake_call
    quota.block("gemini", "402")
    behaviour[llm.models_for("checker")[0]] = '{"changes": []}'
    with pytest.raises(RetryLater):
        llm.generate(session, "p", role="checker", step="check")
    assert _outcomes(session) == [(llm.models_for("checker")[0], "over_budget")]


def test_writer_waits_when_gemini_is_blocked_and_groq_cannot_fit_the_prompt(session, fake_call):
    behaviour, _ = fake_call
    primary, fallback = llm.models_for("writer")
    quota.block("gemini", "402")
    behaviour[fallback] = ValueError("413 request too large for 8K tokens/min")
    with pytest.raises(RetryLater):  # waits for Gemini to come back instead of failing the video
        llm.generate(session, "p", role="writer", step="script", json_mode=False)


def test_daily_quota_429_is_not_retried(session, monkeypatch):
    from google.genai import errors as genai_errors

    calls = _gemini_raising(monkeypatch, genai_errors.ClientError(
        429, {"error": {"message": "Quota exceeded: GenerateRequestsPerDayPerProjectPerModel-FreeTier"}}))
    with pytest.raises(QuotaExceeded):
        llm._generate_with_retry("gemini-3.8-flash", "p", None)
    assert calls == ["gemini-3.8-flash"]
    with pytest.raises(QuotaExceeded):
        quota.check(session, "gemini", "gemini-3.8-flash")
    quota.check(session, "gemini", "gemini-3.5-flash")  # other models keep their own quota


def test_per_model_daily_cap_from_config(session, monkeypatch):
    monkeypatch.setattr(get_config().llm, "daily_requests", {"gemini-3.8-flash": 2})
    for _ in range(2):
        session.add(LlmCall(provider="gemini", model="gemini-3.8-flash", step="script", outcome="ok"))
    session.add(LlmCall(provider="gemini", model="gemini-3.5-flash", step="check", outcome="ok"))
    session.flush()
    with pytest.raises(QuotaExceeded):
        quota.check(session, "gemini", "gemini-3.8-flash")
    quota.check(session, "gemini", "gemini-3.5-flash")


def test_gemini_day_starts_at_midnight_pacific():
    from datetime import UTC, datetime

    # 2026-09-30 06:59 UTC is still 23:59 on the 29th in California (PDT, UTC-7)
    start = quota.day_start("gemini", datetime(2026, 9, 30, 6, 59, tzinfo=UTC))
    assert (start.day, start.hour, start.utcoffset().total_seconds()) == (29, 0, -7 * 3600)
    assert quota.day_start("gemini", datetime(2026, 9, 30, 7, 1, tzinfo=UTC)).day == 30
