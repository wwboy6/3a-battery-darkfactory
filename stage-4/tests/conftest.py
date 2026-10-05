"""Pytest fixtures for the Pocketful stage-1 HTTP suite.

The whole suite talks to an already-running service. Use:

    POCKETFUL_BASE_URL=http://localhost:8080 pytest -q

Every test starts from the canonical fixture unless it resets explicitly.
"""

from __future__ import annotations

import os
import time

import pytest

from support import Api, make_fixture, login, tokens_for


@pytest.fixture(scope="session", autouse=True)
def service_ready():
    """Wait until /health reports ready (within the spec's 60 s budget)."""
    api = Api()
    deadline = time.monotonic() + float(os.environ.get("POCKETFUL_READY_TIMEOUT", "60"))
    last_error = None
    while time.monotonic() < deadline:
        try:
            resp = api.get("/health", timeout=5)
            if resp.status_code == 200:
                try:
                    if resp.json().get("status") == "ok":
                        return
                except ValueError:
                    pass
        except Exception as exc:  # noqa: BLE001 - retried until deadline
            last_error = exc
        time.sleep(0.5)
    pytest.fail(f"service did not become healthy within the timeout: {last_error!r}")


@pytest.fixture
def api():
    return Api()


@pytest.fixture
def reset(api):
    """Callable that POSTs a fixture to /_test/reset and asserts the 204."""

    def _reset(fixture):
        resp = api.post("/_test/reset", body=fixture)
        assert resp.status_code == 204, (
            f"reset failed: {resp.status_code} {resp.text[:300]}"
        )
        return resp

    return _reset


@pytest.fixture(autouse=True)
def default_state(reset):
    """Independent, deterministic starting point for every test."""
    return reset(make_fixture())


@pytest.fixture
def tokens(api):
    """Bearer tokens for the three seeded users (ada, bob, cy)."""
    return tokens_for(api)


@pytest.fixture
def ada_token(tokens):
    return tokens["ada"]


@pytest.fixture
def bob_token(tokens):
    return tokens["bob"]


@pytest.fixture
def cy_token(tokens):
    return tokens["cy"]
