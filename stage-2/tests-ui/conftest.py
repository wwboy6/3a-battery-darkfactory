"""Pytest + Playwright fixtures for the stage-2 browser suite.

By default the suite starts the service under test itself
(``uvicorn main:app`` from ``src/``). Set ``POCKETFUL_BASE_URL`` to point it at
an already-running instance instead.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
from playwright.sync_api import expect, sync_playwright

from support import Api, DEFAULT_PASSWORD, login_ui, make_fixture

STAGE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_VIEWPORT = {"width": 1280, "height": 900}
UI_TIMEOUT = float(os.environ.get("POCKETFUL_UI_TIMEOUT", "15000"))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(base: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            resp = requests.get(base + "/health", timeout=2)
            if resp.status_code == 200 and resp.json().get("status") == "ok":
                return
        except Exception as exc:  # noqa: BLE001 - retried until deadline
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"service did not become healthy within {timeout}s: {last_error!r}")


@pytest.fixture(scope="session")
def base_url():
    """Base URL of the service under test, starting it when necessary."""
    configured = os.environ.get("POCKETFUL_BASE_URL", "").strip()
    if configured:
        base = configured.rstrip("/")
        _wait_for_health(base, float(os.environ.get("POCKETFUL_READY_TIMEOUT", "60")))
        yield base
        return

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["PORT"] = str(port)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
        ],
        cwd=str(STAGE_DIR / "src"),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_health(base, float(os.environ.get("POCKETFUL_READY_TIMEOUT", "60")))
        if proc.poll() is not None:  # pragma: no cover - diagnostic path
            raise RuntimeError(f"service exited early with code {proc.returncode}")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - diagnostic path
            proc.kill()


@pytest.fixture(scope="session", autouse=True)
def _playwright_context_manager():
    with sync_playwright() as playwright:
        expect.set_options(timeout=UI_TIMEOUT)
        yield playwright


@pytest.fixture(scope="session")
def browser(_playwright_context_manager):
    headless = os.environ.get("POCKETFUL_HEADLESS", "1") != "0"
    browser = _playwright_context_manager.chromium.launch(headless=headless)
    yield browser
    browser.close()


@pytest.fixture
def api(base_url):
    return Api(base_url)


@pytest.fixture
def reset(api):
    """Callable that installs a fixture via ``POST /_test/reset``."""

    def _reset(fixture):
        resp = api.post("/_test/reset", body=fixture)
        assert resp.status_code == 204, f"reset failed: {resp.status_code} {resp.text[:300]}"
        return resp

    return _reset


@pytest.fixture(autouse=True)
def default_state(reset):
    """Independent, deterministic starting point for every test."""
    return reset(make_fixture())


@pytest.fixture
def make_page(browser, base_url):
    """Factory for isolated, unauthenticated pages with a fresh cookie jar."""
    contexts = []

    def _make(viewport=None):
        context = browser.new_context(
            base_url=base_url, viewport=viewport or dict(DEFAULT_VIEWPORT)
        )
        contexts.append(context)
        page = context.new_page()
        page.set_default_timeout(UI_TIMEOUT)
        page.set_default_navigation_timeout(UI_TIMEOUT)
        page.on("dialog", lambda dialog: dialog.accept())
        return page

    yield _make
    for context in contexts:
        context.close()


@pytest.fixture
def page(make_page):
    """A fresh, signed-out page."""
    return make_page()


@pytest.fixture
def signed_in(make_page):
    """Factory that returns a page signed in as a seeded user via the login UI."""

    def _signed_in(handle="ada", viewport=None, *, password=DEFAULT_PASSWORD, email=None):
        page = make_page(viewport)
        login_ui(page, email or f"{handle}@example.com", password)
        return page

    return _signed_in
