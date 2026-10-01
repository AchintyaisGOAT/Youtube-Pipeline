"""Test-wide safety nets: no test may reach the network or write into the real data/.

Your real API keys live in .env, so an un-faked call would silently spend free-tier
quota (it happened once, before this file existed). Every HTTP client used here — our
own httpx client, the openai SDK (Groq) and google-genai — sends through httpx, so
blocking `httpx.Client.send` catches all of them. Tests fake the boundary they need
(`llm.generate`, `llm._call`, the wikipedia helpers) instead.

Every stage writes through app.storage, so pointing its base at a temp folder keeps test
runs from leaving folders in data/output/ (it once left 150 of them).
"""

from __future__ import annotations

import httpx
import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(self, request, *args, **kwargs):
        raise RuntimeError(f"network access in tests is disabled: {request.method} {request.url}")

    monkeypatch.setattr(httpx.Client, "send", refuse)


@pytest.fixture(autouse=True)
def _fresh_quota_state():
    """Refusals remembered for the rest of a run (app/quota.py) must not leak between tests."""
    from app import quota

    quota.reset()
    yield
    quota.reset()


@pytest.fixture(autouse=True)
def _temp_storage(tmp_path, monkeypatch):
    monkeypatch.setattr("app.storage._storage_base", lambda: tmp_path / "data")
