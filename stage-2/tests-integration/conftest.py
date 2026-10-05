"""Pytest fixtures for the Pocketful stage-2 integration suite.

The suite talks to an already-running service and never imports its source:

    POCKETFUL_BASE_URL=http://localhost:8080 pytest -q tests-integration

Every test starts from the canonical stage-1-style fixture unless it resets
explicitly, so tests are independent, deterministic and order-insensitive.
"""

from __future__ import annotations

import os
import time

import pytest

from support import (
    Api,
    Browser,
    DEFAULT_PASSWORD,
    form_login,
    make_fixture,
    tokens_for,
)


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


@pytest.fixture
def browser():
    """A fresh cookie-carrying browser client (not signed in)."""
    return Browser()


@pytest.fixture
def login_browser():
    """Factory: sign a seeded user in through the HTML ``/login`` form.

    Each call returns a **fresh** :class:`Browser` with its own cookie jar, so
    tests that act as several users never leak one session into another.
    """

    def _login(handle, password=DEFAULT_PASSWORD):
        client = Browser()
        resp = form_login(client, f"{handle}@example.com", password)
        assert client.signed_in(), (
            f"login for {handle!r} did not set a session cookie: "
            f"{resp.status_code} {resp.text[:200]}"
        )
        return client, resp

    return _login
