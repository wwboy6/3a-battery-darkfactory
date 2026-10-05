"""Required routes, cookie-based browser auth, and HTML/JSON negotiation.

These tests drive the browser surface the way a person would: GET an HTML page,
fill the rendered form, submit it, and check the signed-in pages. They also pin
the content-negotiation contract that lets the same route serve UI and API.
"""

from __future__ import annotations

import re

from support import (
    DEFAULT_PASSWORD,
    assert_absent,
    expect,
    find_by_testid,
    form_login,
    form_signup,
    has_testid,
    make_fixture,
    parse_html,
    payload,
    require_testid,
    seeded_request,
    testid_attr,
    testid_text,
    unique,
)

WALLET_TESTIDS = ("wallet-balance", "wallet-available")
PAY_FORM_TESTIDS = ("pay-handle", "pay-amount", "pay-note", "pay-visibility", "pay-submit")
REQUEST_FORM_TESTIDS = (
    "request-handle",
    "request-amount",
    "request-note",
    "request-submit",
)


def derive_handle(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    return re.sub(r"[^a-z0-9_]", "_", local)[:20]


def is_html(response) -> bool:
    return response.headers.get("content-type", "").startswith("text/html")


def is_json(response) -> bool:
    return response.headers.get("content-type", "").startswith("application/json")


# --------------------------------------------------------------------------- #
# Static HTML routes
# --------------------------------------------------------------------------- #


def test_login_page_renders_required_fields(browser):
    resp, document = browser.page("/login")
    expect(resp, 200)
    assert is_html(resp)
    for testid in ("login-email", "login-password", "login-submit"):
        require_testid(document, testid)
    assert_absent(document, "current-user")


def test_signup_page_renders_required_fields(browser):
    resp, document = browser.page("/signup")
    expect(resp, 200)
    for testid in (
        "signup-email",
        "signup-password",
        "signup-display-name",
        "signup-submit",
    ):
        require_testid(document, testid)
    assert testid_attr(document, "signup-password", "type") == "password"


def test_wallet_page_renders_required_forms(login_browser):
    browser, _ = login_browser("ada")
    resp, document = browser.page("/")
    expect(resp, 200)
    assert is_html(resp)
    for testid in WALLET_TESTIDS + PAY_FORM_TESTIDS + REQUEST_FORM_TESTIDS + ("wallet-refresh",):
        require_testid(document, testid)
    visibility = require_testid(document, "pay-visibility")
    option_values = {
        node.attrs.get("value")
        for node in visibility.children
        if node.tag == "option"
    }
    assert {"public", "private"} <= option_values


def test_split_page_renders_required_form(login_browser):
    browser, _ = login_browser("ada")
    resp, document = browser.page("/split")
    expect(resp, 200)
    for testid in (
        "split-amount",
        "split-handles",
        "split-note",
        "split-submit",
        "split-preview",
    ):
        require_testid(document, testid)


# --------------------------------------------------------------------------- #
# Form login / signup and the session cookie
# --------------------------------------------------------------------------- #


def test_form_login_signs_browser_in(login_browser):
    browser, resp = login_browser("ada")
    assert browser.signed_in()
    page = browser.get("/", accept="text/html")
    document = parse_html(page.text)
    expect(page, 200)
    require_testid(document, "current-user")
    assert "Ada" in testid_text(document, "current-user")
    assert testid_text(document, "current-handle") == "ada"
    require_testid(document, "logout-button")


def test_current_handle_is_bare_handle_without_at(login_browser):
    browser, _ = login_browser("bob")
    document = parse_html(browser.get("/", accept="text/html").text)
    handle = testid_text(document, "current-handle")
    assert handle == "bob"
    assert "@" not in handle


def test_form_signup_creates_account_and_holds_session(browser, api):
    email = f"{unique('itest')}@example.com"
    resp = form_signup(browser, email, DEFAULT_PASSWORD, "Itest Person")
    assert browser.signed_in(), f"signup did not set a cookie: {resp.status_code}"
    page = browser.get("/", accept="text/html")
    document = parse_html(page.text)
    expect(page, 200)
    assert "Itest Person" in testid_text(document, "current-user")
    assert testid_text(document, "current-handle") == derive_handle(email)

    # The same account can log in through the JSON API: one identity, two surfaces.
    login = api.post("/auth/login", body={"email": email, "password": DEFAULT_PASSWORD})
    expect(login, 200)
    token = payload(login)["token"]
    me = payload(expect(api.get("/me", token=token), 200))
    assert me["handle"] == derive_handle(email)


# --------------------------------------------------------------------------- #
# Auth error states
# --------------------------------------------------------------------------- #


def test_bad_password_shows_auth_error(browser):
    resp = form_login(browser, "ada@example.com", "definitely-wrong")
    assert resp.status_code < 500
    document = parse_html(resp.text)
    error = require_testid(document, "auth-error")
    assert error.text(), "auth-error must carry visible text"
    assert_absent(document, "current-user")


def test_duplicate_signup_shows_auth_error(browser):
    resp = form_signup(browser, "ada@example.com")
    assert resp.status_code < 500
    document = parse_html(resp.text)
    assert require_testid(document, "auth-error").text()


def test_short_password_signup_shows_auth_error(browser):
    resp = form_signup(browser, f"{unique('short')}@example.com", "short")
    assert resp.status_code < 500
    document = parse_html(resp.text)
    assert require_testid(document, "auth-error").text()


def test_unsigned_wallet_route_does_not_leak_user_data(browser):
    resp = browser.get("/", accept="text/html")
    document = parse_html(resp.text)
    assert_absent(document, "current-user")
    assert_absent(document, "wallet-balance")
    assert not find_by_testid(document, "current-handle")


# --------------------------------------------------------------------------- #
# Content negotiation on shared routes
# --------------------------------------------------------------------------- #


def test_requests_html_for_browser_and_json_for_api(login_browser, api, reset, seed):
    tokens = seed(make_fixture(request_list=[seeded_request("rq_1", "u_bob", "u_ada", 1200, "taxi")]))
    browser, _ = login_browser("ada")

    page = browser.get("/requests", accept="text/html")
    expect(page, 200)
    assert is_html(page)
    document = parse_html(page.text)
    require_testid(document, "incoming-list")
    require_testid(document, "outgoing-list")
    item = require_testid(document, "request-item-rq_1")
    assert item.attrs.get("data-status") == "pending"
    assert testid_text(document, "request-amount-rq_1") == "12.00 EUR"
    require_testid(document, "request-pay-rq_1")

    # The API stays JSON: no Accept: text/html.
    api_resp = api.get("/requests", token=tokens["ada"])
    expect(api_resp, 200)
    assert is_json(api_resp)
    body = payload(api_resp)
    assert [r["request_id"] for r in body["requests"]] == ["rq_1"]


def test_requests_default_to_json_without_html_accept(api, reset, seed):
    tokens = seed(make_fixture(request_list=[seeded_request("rq_1", "u_bob", "u_ada", 1200)]))
    resp = api.get("/requests", token=tokens["ada"], headers={"Accept": "application/json"})
    expect(resp, 200)
    assert is_json(resp)
    assert "requests" in payload(resp)


def test_requests_html_requires_authentication(browser):
    resp = browser.get("/requests", accept="text/html")
    document = parse_html(resp.text)
    assert_absent(document, "incoming-list")


def test_authorizations_html_for_browser_and_json_for_api(login_browser, api, reset, seed):
    from support import seeded_authorization

    tokens = seed(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    browser, _ = login_browser("ada")

    page = browser.get("/authorizations", accept="text/html")
    expect(page, 200)
    assert is_html(page)
    document = parse_html(page.text)
    require_testid(document, "authorization-list")
    require_testid(document, "authorization-item-a_1")

    api_resp = api.get("/authorizations", token=tokens["ada"])
    expect(api_resp, 200)
    assert is_json(api_resp)
    ids = [a["authorization_id"] for a in payload(api_resp)["authorizations"]]
    assert ids == ["a_1"]


def test_empty_requests_state_is_rendered(login_browser):
    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/requests", accept="text/html").text)
    assert has_testid(document, "empty-requests")
    assert_absent(document, "request-item-rq_1")
