"""Shared helpers for the Pocketful stage-2 browser (Playwright) suite.

The suite drives a real browser against a running service. Only the public HTTP
API and the rendered HTML (via the ``data-testid`` contract) are used; nothing
here imports the implementation.

Point the suite at a running instance with ``POCKETFUL_BASE_URL``; when that is
unset ``conftest.py`` starts ``src/main.py`` itself.
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
from decimal import Decimal, InvalidOperation

import requests

DEFAULT_PASSWORD = "correct horse"
DEFAULT_TIMEOUT = float(os.environ.get("POCKETFUL_HTTP_TIMEOUT", "10"))

_UNSET = object()
_counter = itertools.count(1)


def unique(prefix: str = "k") -> str:
    """Process-unique token for idempotency keys."""
    return f"{prefix}-{next(_counter)}-{os.getpid()}"


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #


class Api:
    """Thin HTTP client for the service under test."""

    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        token=None,
        idem=None,
        params=None,
        body=_UNSET,
        headers=None,
    ):
        hdrs = dict(headers or {})
        if token is not None:
            hdrs["Authorization"] = f"Bearer {token}"
        if idem is not None:
            hdrs["Idempotency-Key"] = idem
        kwargs = {"headers": hdrs, "params": params, "timeout": self.timeout}
        if body is not _UNSET:
            kwargs["json"] = body
        return requests.request(method, self.base_url + path, **kwargs)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)


def payload(response):
    """Parsed JSON body, failing loudly when it is not JSON."""
    try:
        return response.json()
    except ValueError as exc:  # pragma: no cover - diagnostic path
        raise AssertionError(
            f"expected JSON body, got {response.status_code}: {response.text[:300]!r}"
        ) from exc


def error_code(response):
    try:
        return response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


def api_login(api: Api, handle: str, password: str = DEFAULT_PASSWORD) -> str:
    """Return a bearer token for a seeded user by logging in over HTTP."""
    resp = api.post("/auth/login", body={"email": f"{handle}@example.com", "password": password})
    assert resp.status_code == 200, f"login failed for {handle}: {resp.status_code} {resp.text[:300]}"
    return payload(resp)["token"]


# --------------------------------------------------------------------------- #
# Fixture builders (mirroring the stage-2 fixture format)
# --------------------------------------------------------------------------- #


def user(uid, handle, balance=0, *, email=None, password=DEFAULT_PASSWORD, display_name=None):
    return {
        "id": uid,
        "email": email or f"{handle}@example.com",
        "password": password,
        "display_name": display_name or handle.capitalize(),
        "handle": handle,
        "balance": balance,
    }


def default_users():
    return [
        user("u_ada", "ada", 10000),
        user("u_bob", "bob", 2500),
        user("u_cy", "cy", 0),
    ]


def make_fixture(
    users=None,
    payments=None,
    requests_=None,
    authorizations=None,
    *,
    currency="EUR",
    minor_units=2,
    authorization_ttl_seconds=None,
):
    fixture = {
        "currency": currency,
        "minor_units": minor_units,
        "users": default_users() if users is None else users,
    }
    if authorization_ttl_seconds is not None:
        fixture["authorization_ttl_seconds"] = authorization_ttl_seconds
    if payments:
        fixture["payments"] = payments
    if requests_:
        fixture["requests"] = requests_
    if authorizations is not None:
        fixture["authorizations"] = authorizations
    return fixture


def seeded_payment(pid, from_user_id, to_user_id, amount, note="", visibility="public"):
    return {
        "id": pid,
        "from_user_id": from_user_id,
        "to_user_id": to_user_id,
        "amount": amount,
        "note": note,
        "visibility": visibility,
    }


def seeded_request(rid, requester_id, payer_id, amount, note="", status="pending"):
    return {
        "id": rid,
        "requester_id": requester_id,
        "payer_id": payer_id,
        "amount": amount,
        "note": note,
        "status": status,
    }


def seeded_authorization(
    aid,
    from_user_id,
    to_user_id,
    amount,
    *,
    note="",
    visibility="public",
    status="open",
    expires_at,
):
    return {
        "id": aid,
        "from_user_id": from_user_id,
        "to_user_id": to_user_id,
        "amount": amount,
        "note": note,
        "visibility": visibility,
        "status": status,
        "expires_at": expires_at,
    }


# --------------------------------------------------------------------------- #
# Time and money formatting
# --------------------------------------------------------------------------- #


def iso(offset_seconds: float = 0.0) -> str:
    """RFC 3339 UTC timestamp offset from now, second precision."""
    moment = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_seconds)
    return moment.replace(microsecond=0).isoformat()


def rfc3339_equal(left: str, right: str) -> bool:
    """True when two RFC 3339 strings denote the same instant."""

    def _parse(value):
        return dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))

    try:
        return _parse(left) == _parse(right)
    except ValueError:
        return False


def money(minor: int, currency: str = "EUR", minor_units: int = 2) -> str:
    """Exact ``wallet-*`` text: ``100.00 EUR`` / ``1200 JPY`` / ``1.500 BHD``."""
    minor = int(minor)
    if minor_units <= 0:
        return f"{minor} {currency}"
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    scale = 10 ** minor_units
    whole, frac = divmod(minor, scale)
    return f"{sign}{whole}.{frac:0{minor_units}d} {currency}"


def parse_decimal_to_minor(text: str, minor_units: int) -> int:
    """Parse a decimal input value (e.g. ``20.00``) back to minor units."""
    cleaned = str(text).strip()
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError) as exc:
        raise AssertionError(f"not a decimal value: {text!r}") from exc
    quantum = Decimal(1).scaleb(-minor_units)
    quantized = value.quantize(quantum)
    if quantized != value:
        raise AssertionError(f"more than {minor_units} decimal places: {text!r}")
    return int(quantized.scaleb(minor_units))


# --------------------------------------------------------------------------- #
# Browser helpers
# --------------------------------------------------------------------------- #


def login_ui(page, email: str, password: str = DEFAULT_PASSWORD) -> None:
    """Sign in through the real login screen and wait for the app shell."""
    page.goto("/login")
    page.get_by_test_id("login-email").fill(email)
    page.get_by_test_id("login-password").fill(password)
    page.get_by_test_id("login-submit").click()
    page.get_by_test_id("current-user").wait_for(state="visible")


def signup_ui(page, email: str, password: str = DEFAULT_PASSWORD, display_name: str = "New User") -> None:
    page.goto("/signup")
    page.get_by_test_id("signup-email").fill(email)
    page.get_by_test_id("signup-password").fill(password)
    page.get_by_test_id("signup-display-name").fill(display_name)
    page.get_by_test_id("signup-submit").click()


def logout_ui(page) -> None:
    page.get_by_test_id("logout-button").click()
    page.get_by_test_id("login-submit").wait_for(state="visible")


def ensure_authorize_form(page) -> None:
    """Make the authorize form visible.

    The spec puts the authorize form on the authorizations screen; tolerate an
    implementation that keeps it on ``/`` as long as the ``data-testid`` exists.
    """
    page.goto("/authorizations")
    if page.get_by_test_id("authorize-handle").count() == 0:
        page.goto("/")
    page.get_by_test_id("authorize-handle").wait_for(state="visible")


def fill_pay_form(page, handle: str, amount: str, note: str = "", visibility=None) -> None:
    page.get_by_test_id("pay-handle").fill(handle)
    page.get_by_test_id("pay-amount").fill(amount)
    page.get_by_test_id("pay-note").fill(note)
    if visibility is not None:
        page.get_by_test_id("pay-visibility").select_option(visibility)


def fill_request_form(page, handle: str, amount: str, note: str = "") -> None:
    page.get_by_test_id("request-handle").fill(handle)
    page.get_by_test_id("request-amount").fill(amount)
    page.get_by_test_id("request-note").fill(note)


def fill_authorize_form(page, handle: str, amount: str, note: str = "", visibility=None) -> None:
    page.get_by_test_id("authorize-handle").fill(handle)
    page.get_by_test_id("authorize-amount").fill(amount)
    page.get_by_test_id("authorize-note").fill(note)
    if visibility is not None:
        page.get_by_test_id("authorize-visibility").select_option(visibility)


def wallet_text(page, testid: str = "wallet-balance") -> str:
    return page.get_by_test_id(testid).inner_text().strip()


def path_of(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).path


def is_post_to(target, path: str) -> bool:
    """True when a Playwright Request or Response is a POST to ``path``.

    ``page.expect_response`` passes a Response while ``page.on("request")``
    passes a Request, so accept either (a Response exposes the originating
    request as ``.request``).
    """
    request = getattr(target, "request", target)
    return request.method == "POST" and path_of(request.url) == path


def locator_by_prefix(page, prefix: str):
    return page.locator(f'[data-testid^="{prefix}"]')


def dom_testid_order(page, prefix: str):
    return locator_by_prefix(page, prefix).evaluate_all(
        "els => els.map(e => e.getAttribute('data-testid'))"
    )


def horizontal_overflow(page) -> int:
    return page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )


def accessible_name(page, testid: str) -> str:
    return page.get_by_test_id(testid).evaluate(
        """el => {
            const aria = (el.getAttribute('aria-label') || '').trim();
            if (aria) return aria;
            const labelledby = el.getAttribute('aria-labelledby');
            if (labelledby) {
                const parts = labelledby.split(/\\s+/).map(id => {
                    const n = document.getElementById(id);
                    return n ? (n.textContent || '').trim() : '';
                });
                const joined = parts.filter(Boolean).join(' ').trim();
                if (joined) return joined;
            }
            const labels = el.labels ? Array.from(el.labels) : [];
            return labels.map(l => (l.textContent || '').trim()).filter(Boolean).join(' ').trim();
        }"""
    )
