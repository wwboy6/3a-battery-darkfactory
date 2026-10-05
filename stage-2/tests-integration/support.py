"""Shared helpers for the Pocketful stage-2 integration suite.

The suite drives a *running* stage-2 service over HTTP only. It combines two
surfaces that must agree:

* the server-rendered HTML the browser sees (``Accept: text/html``), and
* the JSON API the embedded ``static/app.js`` calls.

Nothing here imports the implementation's source code: the submission is a
container and only its HTTP behaviour is under test. Point the suite at a
running instance with ``POCKETFUL_BASE_URL`` (default ``http://localhost:8080``).
"""

from __future__ import annotations

import itertools
import os
import re
import threading
from html.parser import HTMLParser

import requests

BASE_URL = os.environ.get("POCKETFUL_BASE_URL", "http://localhost:8080").rstrip("/")
DEFAULT_TIMEOUT = float(os.environ.get("POCKETFUL_HTTP_TIMEOUT", "10"))

DEFAULT_PASSWORD = "correct horse"

_UNSET = object()
_counter = itertools.count(1)

# Seeded expiry values: well over an hour away from any plausible reset time,
# one in the far future (an effective open hold) and one in the past.
FAR_FUTURE = "2999-01-01T00:00:00+00:00"
LONG_AGO = "2000-01-01T00:00:00+00:00"


def unique(prefix: str = "k") -> str:
    """Return a process-unique token for idempotency keys / handles / emails."""
    return f"{prefix}-{next(_counter)}-{os.getpid()}"


# --------------------------------------------------------------------------- #
# JSON HTTP client
# --------------------------------------------------------------------------- #


class Api:
    """Thin, stateless JSON client for the service under test."""

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
            kwargs["data"] = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
        elif body is not _UNSET:
            kwargs["json"] = body
            hdrs.setdefault("Content-Type", "application/json")
        return requests.request(method, self.url(path), **kwargs)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)


# --------------------------------------------------------------------------- #
# Cookie-carrying browser client
# --------------------------------------------------------------------------- #


class Browser:
    """A cookie jar plus a small form-filling client, i.e. a headless browser.

    HTML routes authenticate from an HttpOnly session cookie set by the
    ``/signup`` and ``/login`` form handlers, so a bare :class:`Api` cannot see
    them. This client keeps that cookie jar.
    """

    def __init__(self, base_url: str = BASE_URL, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def get(self, path: str, *, accept=None, headers=None, timeout=None, **kwargs):
        hdrs = dict(headers or {})
        if accept is not None:
            hdrs.setdefault("Accept", accept)
        return self.session.get(
            self.url(path), headers=hdrs, timeout=timeout or self.timeout, **kwargs
        )

    def post_form(self, path: str, fields, *, follow=True, timeout=None, headers=None):
        hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
        hdrs.update(headers or {})
        return self.session.post(
            self.url(path),
            data=fields,
            headers=hdrs,
            allow_redirects=follow,
            timeout=timeout or self.timeout,
        )

    def page(self, path: str):
        """GET an HTML route and return ``(response, parsed_document)``."""
        resp = self.get(path, accept="text/html")
        return resp, parse_html(resp.text)

    def signed_in(self) -> bool:
        """Whether the cookie jar currently authenticates a session."""
        return bool(self.session.cookies)


def extract_form_fields(document, values):
    """Build a form payload from ``{testid: value}`` using each input's ``name``.

    Field names are the frontend's choice, so read them from the rendered HTML
    instead of hard-coding them. ``values`` may map a testid to ``None`` to skip
    it (useful to omit an optional field).
    """
    fields = {}
    for testid, value in values.items():
        node = find_by_testid(document, testid)
        assert node is not None, f"form field {testid!r} not present in the page"
        name = node.attrs.get("name")
        assert name, f"form field {testid!r} has no name attribute to submit under"
        if value is not None:
            fields[name] = value
    return fields


def form_login(browser: Browser, email: str, password: str = DEFAULT_PASSWORD):
    """Log in through the HTML form (``/login``), populating the cookie jar."""
    _, document = browser.page("/login")
    fields = extract_form_fields(
        document, {"login-email": email, "login-password": password}
    )
    return browser.post_form("/login", fields)


def form_signup(
    browser: Browser,
    email: str,
    password: str = DEFAULT_PASSWORD,
    display_name: str = "New User",
):
    """Sign up through the HTML form (``/signup``), then hold the session cookie."""
    _, document = browser.page("/signup")
    fields = extract_form_fields(
        document,
        {
            "signup-email": email,
            "signup-password": password,
            "signup-display-name": display_name,
        },
    )
    return browser.post_form("/signup", fields)


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


def expect_ok(response):
    assert 200 <= response.status_code < 300, (
        f"expected 2xx, got {response.status_code}: {response.text[:500]}"
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


# --------------------------------------------------------------------------- #
# HTML parsing and data-testid helpers
# --------------------------------------------------------------------------- #

_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class HtmlNode:
    """A parsed element with attributes, children and raw text parts."""

    __slots__ = ("tag", "attrs", "children", "parts", "parent")

    def __init__(self, tag, attrs, parent=None):
        self.tag = tag
        self.attrs = attrs
        self.children = []
        self.parts = []
        self.parent = parent

    def text(self) -> str:
        chunks = []

        def walk(node):
            chunks.extend(node.parts)
            for child in node.children:
                walk(child)

        walk(self)
        return _normalize("".join(chunks))

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<HtmlNode {self.tag} testid={self.attrs.get('data-testid')!r}>"


def _normalize(value: str) -> str:
    return " ".join(value.split())


class _HtmlParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("#document", {})
        self.stack = [self.root]
        self.skip = 0

    def _attrs(self, attrs):
        return {key: ("" if val is None else val) for key, val in attrs}

    def handle_starttag(self, tag, attrs):
        node = HtmlNode(tag, self._attrs(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag in _VOID:
            return
        self.stack.append(node)
        if tag in ("script", "style"):
            self.skip += 1

    def handle_startendtag(self, tag, attrs):
        node = HtmlNode(tag, self._attrs(attrs), self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                if tag in ("script", "style") and self.skip:
                    self.skip -= 1
                del self.stack[index:]
                return

    def handle_data(self, data):
        if self.skip:
            return
        self.stack[-1].parts.append(data)


def parse_html(text: str) -> HtmlNode:
    parser = _HtmlParser()
    parser.feed(text)
    parser.close()
    return parser.root


def iter_nodes(node):
    """Iterate ``node`` and every descendant in document order."""
    yield node
    for child in node.children:
        yield from iter_nodes(child)


def testids_in_order(document) -> list:
    return [
        n.attrs["data-testid"]
        for n in iter_nodes(document)
        if "data-testid" in n.attrs
    ]


def has_testid(document, testid: str) -> bool:
    return find_by_testid(document, testid) is not None


def find_by_testid(document, testid: str):
    for node in iter_nodes(document):
        if node.attrs.get("data-testid") == testid:
            return node
    return None


def find_all_by_testid(document, testid: str) -> list:
    return [n for n in iter_nodes(document) if n.attrs.get("data-testid") == testid]


def testid_text(document, testid: str) -> str:
    node = find_by_testid(document, testid)
    assert node is not None, f"missing data-testid={testid!r} (page had: {testids_in_order(document)[:40]})"
    return node.text()


def testid_attr(document, testid: str, name: str, default=None):
    node = find_by_testid(document, testid)
    assert node is not None, f"missing data-testid={testid!r}"
    return node.attrs.get(name, default)


def require_testid(document, testid: str):
    node = find_by_testid(document, testid)
    assert node is not None, (
        f"missing data-testid={testid!r} (page had: {testids_in_order(document)[:40]})"
    )
    return node


def assert_absent(document, testid: str):
    node = find_by_testid(document, testid)
    assert node is None, f"data-testid={testid!r} should be absent but was rendered"

# These helpers are imported into test modules; their ``test`` prefix must not
# make pytest collect them as tests.
for _helper in (testid_text, testid_attr, testids_in_order):
    _helper.__test__ = False
del _helper


# --------------------------------------------------------------------------- #
# Money formatting (mirrors stage-1 §4 / stage-2 "Formatted amount")
# --------------------------------------------------------------------------- #


def money_text(minor: int, minor_units: int, currency: str) -> str:
    """Return the exact formatted amount the UI must show."""
    if minor_units == 0:
        return f"{minor} {currency}"
    sign = "-" if minor < 0 else ""
    scale = 10 ** minor_units
    whole, frac = divmod(abs(minor), scale)
    return f"{sign}{whole}.{str(frac).zfill(minor_units)} {currency}"


# --------------------------------------------------------------------------- #
# Fixture builders (stage 1 + stage 2)
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
    authorizations=None,
    *,
    currency="EUR",
    minor_units=2,
    authorization_ttl_seconds=_UNSET,
    settlement_operator_ids=None,
):
    """Build a reset fixture, optionally with the stage-2 authorization fields."""
    fixture = {
        "currency": currency,
        "minor_units": minor_units,
        "users": default_users() if users is None else users,
    }
    if payments:
        fixture["payments"] = payments
    if request_list:
        fixture["requests"] = request_list
    if authorizations is not None:
        fixture["authorizations"] = authorizations
    if authorization_ttl_seconds is not _UNSET:
        fixture["authorization_ttl_seconds"] = authorization_ttl_seconds
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


def seeded_authorization(
    aid,
    from_user_id,
    to_user_id,
    amount,
    *,
    note="",
    visibility="public",
    status="open",
    expires_at=FAR_FUTURE,
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
# Auth helpers
# --------------------------------------------------------------------------- #


def login(api, email, password=DEFAULT_PASSWORD):
    return api.post("/auth/login", body={"email": email, "password": password})


def tokens_for(api, handles=("ada", "bob", "cy")):
    out = {}
    for handle in handles:
        resp = login(api, f"{handle}@example.com")
        assert resp.status_code == 200, f"seeded login failed for {handle}: {resp.text[:300]}"
        out[handle] = resp.json()["token"]
    return out


def me(api, token):
    resp = api.get("/me", token=token)
    expect(resp, 200)
    return payload(resp)


def total_of(api, token):
    data = me(api, token)
    assert data["balance"] == data["total"], "balance and total must agree"
    return data["total"]


# --------------------------------------------------------------------------- #
# Concurrency helper
# --------------------------------------------------------------------------- #


def run_concurrently(count, func, timeout=60):
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
