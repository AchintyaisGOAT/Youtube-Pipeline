"""Test-wide safety net: no test may reach the network.

Your real API keys live in .env, so an un-faked call would silently spend free-tier
quota (it happened once, before this file existed). Every HTTP client used here — our
own httpx client, the openai SDK (Groq) and google-genai — sends through httpx, so
blocking `httpx.Client.send` catches all of them. Tests fake the boundary they need
(`llm.generate`, `llm._call`, the wikipedia helpers) instead.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(self, request, *args, **kwargs):
        raise RuntimeError(f"network access in tests is disabled: {request.method} {request.url}")

    monkeypatch.setattr(httpx.Client, "send", refuse)
