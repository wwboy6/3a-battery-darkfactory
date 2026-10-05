"""Shared helpers for the Pocketful stage-1 HTTP test suite.

These tests exercise the service strictly over HTTP. Point them at a running
instance with ``POCKETFUL_BASE_URL`` (default ``http://localhost:8080``).

Nothing here imports or depends on the implementation's source code: the
submission is a container, and only its HTTP behaviour is under test.
"""

from __future__ import annotations

import itertools
import os
import re
import threading

import requests

BASE_URL = os.environ.get("POCKETFUL_BASE_URL", "http://localhost:8080").rstrip("/")
DEFAULT_TIMEOUT = float(os.environ.get("POCKETFUL_HTTP_TIMEOUT", "10"))

DEFAULT_PASSWORD = "correct horse"

_UNSET = object()
_counter = itertools.count(1)


def unique(prefix: str = "k") -> str:
    """Return a process-unique token for idempotency keys / handles."""
    return f"{prefix}-{next(_counter)}-{os.getpid()}"


class Api:
    """Thin HTTP client for the service under test."""

    def __init__(self, base_url: str = BASE_URL, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def request(
        self,
        method: str,
        path: str,
        *,
        token=None,
        idem=None,
        params=None,
        body=_UNSET,
        raw_body=None,
        content_type="application/json",
        headers=None,
        timeout=None,
    ):
        hdrs = dict(headers or {})
        if token is not None:
            hdrs["Authorization"] = f"Bearer {token}"
        if idem is not None:
            hdrs["Idempotency-Key"] = idem

        kwargs = {"headers": hdrs, "params": params, "timeout": timeout or self.timeout}
        if raw_body is not None:
            payload = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
            kwargs["data"] = payload
            if content_type is not None:
                hdrs["Content-Type"] = content_type
        elif body is not _UNSET:
            kwargs["json"] = body
            if content_type is not None:
                hdrs.setdefault("Content-Type", content_type)
        return requests.request(method, self.url(path), **kwargs)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)


# --------------------------------------------------------------------------- #
# Response helpers
# --------------------------------------------------------------------------- #


def payload(response):
    """Return the parsed JSON body (fails loudly on non-JSON)."""
    try:
        return response.json()
    except ValueError as exc:  # pragma: no cover - diagnostic path
        raise AssertionError(
            f"expected JSON body, got {response.status_code}: {response.text[:500]!r}"
        ) from exc


def error_code(response):
    try:
        return response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


def expect(response, status, code=None):
    """Assert an HTTP status and, optionally, an ``error.code``."""
    assert response.status_code == status, (
        f"expected HTTP {status}, got {response.status_code}: {response.text[:500]}"
    )
    if code is not None:
        body = payload(response)
        assert isinstance(body, dict) and isinstance(body.get("error"), dict), (
            f"expected error object, got: {response.text[:500]}"
        )
        err = body["error"]
        assert err.get("code") == code, (
            f"expected error code {code!r}, got {err.get('code')!r}: {response.text[:500]}"
        )
        assert isinstance(err.get("message"), str) and err["message"], (
            "error.message must be a non-empty string"
        )
    return response


RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def assert_rfc3339(value, field="timestamp"):
    assert isinstance(value, str), f"{field} must be a string, got {value!r}"
    assert RFC3339_RE.match(value), (
        f"{field} must be RFC 3339 with an explicit offset, got {value!r}"
    )


def assert_no_error(response):
    assert response.status_code < 400, (
        f"unexpected error {response.status_code}: {response.text[:500]}"
    )


# --------------------------------------------------------------------------- #
# Fixture builders
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
    request_list=None,
    *,
    currency="EUR",
    minor_units=2,
    settlement_operator_ids=None,
):
    fixture = {
        "currency": currency,
        "minor_units": minor_units,
        "users": default_users() if users is None else users,
    }
    if payments:
        fixture["payments"] = payments
    if request_list:
        fixture["requests"] = request_list
    if settlement_operator_ids is not None:
        fixture["settlement_operator_ids"] = settlement_operator_ids
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


# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #


def login(api, email, password=DEFAULT_PASSWORD):
    return api.post("/auth/login", body={"email": email, "password": password})


def signup(api, email, password=DEFAULT_PASSWORD, display_name="New User", **extra):
    body = {"email": email, "password": password, "display_name": display_name}
    body.update(extra)
    return api.post("/auth/signup", body=body)


def tokens_for(api, handles=("ada", "bob", "cy")):
    out = {}
    for handle in handles:
        resp = login(api, f"{handle}@example.com")
        assert resp.status_code == 200, f"seeded login failed for {handle}: {resp.text[:300]}"
        out[handle] = resp.json()["token"]
    return out


# --------------------------------------------------------------------------- #
# Concurrency helper
# --------------------------------------------------------------------------- #


def run_concurrently(count, func, timeout=30):
    """Run ``func(i)`` in ``count`` threads released simultaneously.

    Returns the results in index order. Exceptions are captured and re-raised
    as an ``AssertionError`` naming the originating index.
    """
    barrier = threading.Barrier(count)
    results = [None] * count
    errors = [None] * count

    def worker(index):
        try:
            barrier.wait()
            results[index] = func(index)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the caller
            errors[index] = exc

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout)
    for index, exc in enumerate(errors):
        if exc is not None:
            raise AssertionError(f"concurrent worker {index} failed: {exc!r}") from exc
    return results
