"""Server-rendered authorization screen: fields, state and per-party controls.

The API is the source of truth; the HTML must present the same amount, status,
expiry and captured values, and must offer capture/void only to the correct
party in the correct state.
"""

from __future__ import annotations

from support import (
    LONG_AGO,
    assert_absent,
    assert_rfc3339,
    expect,
    make_fixture,
    me,
    money_text,
    parse_html,
    payload,
    require_testid,
    seeded_authorization,
    testid_attr,
    testid_text,
)


def authorizations_json(api, token, **params):
    resp = api.get("/authorizations", token=token, params=params or None)
    expect(resp, 200)
    return payload(resp)["authorizations"]


def find_authorization(api, token, aid):
    for auth in authorizations_json(api, token):
        if auth["authorization_id"] == aid:
            return auth
    raise AssertionError(f"authorization {aid!r} not returned by GET /authorizations")


def captured_text(document, aid):
    return testid_text(document, f"authorization-captured-{aid}")


# --------------------------------------------------------------------------- #
# Authorize form on the wallet
# --------------------------------------------------------------------------- #


def test_authorize_form_renders_with_required_fields(login_browser):
    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/", accept="text/html").text)
    for testid in (
        "authorize-handle",
        "authorize-amount",
        "authorize-note",
        "authorize-visibility",
        "authorize-submit",
    ):
        require_testid(document, testid)
    visibility = require_testid(document, "authorize-visibility")
    option_values = {
        node.attrs.get("value") for node in visibility.children if node.tag == "option"
    }
    assert {"public", "private"} <= option_values


# --------------------------------------------------------------------------- #
# Open holds
# --------------------------------------------------------------------------- #


def test_open_hold_renders_amount_expiry_and_party_controls(login_browser, api, reset, seed):
    tokens = seed(
        make_fixture(
            authorizations=[
                seeded_authorization(
                    "a_1", "u_ada", "u_bob", 2000, note="deposit", visibility="private"
                )
            ]
        )
    )
    expected = find_authorization(api, tokens["ada"], "a_1")
    assert expected["status"] == "open"
    assert_rfc3339(expected["expires_at"])

    browser, _ = login_browser("ada")  # payer: can void, cannot capture
    document = parse_html(browser.get("/authorizations", accept="text/html").text)
    item = require_testid(document, "authorization-item-a_1")
    assert item.attrs.get("data-status") == "open"
    assert testid_text(document, "authorization-amount-a_1") == "20.00 EUR"
    assert testid_text(document, "authorization-expires-a_1") == expected["expires_at"]
    require_testid(document, "authorization-void-a_1")
    assert_absent(document, "authorization-capture-a_1")
    assert_absent(document, "authorization-capture-amount-a_1")

    browser, _ = login_browser("bob")  # receiver: can capture, cannot void
    document = parse_html(browser.get("/authorizations", accept="text/html").text)
    assert testid_text(document, "authorization-amount-a_1") == "20.00 EUR"
    require_testid(document, "authorization-capture-a_1")
    assert testid_attr(document, "authorization-capture-amount-a_1", "value") == "20.00"
    assert_absent(document, "authorization-void-a_1")


def test_open_hold_prefill_uses_remaining_after_partial_capture(login_browser, api, reset, seed):
    tokens = seed(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    capture = api.post(
        "/authorizations/a_1/capture",
        token=tokens["bob"],
        idem="capture-partial",
        body={"amount": 500, "final": False},
    )
    expect(capture, 201)
    browser, _ = login_browser("bob")
    document = parse_html(browser.get("/authorizations", accept="text/html").text)
    assert testid_attr(document, "authorization-capture-amount-a_1", "value") == "15.00"
    assert require_testid(document, "authorization-item-a_1").attrs.get("data-status") == "open"


def test_empty_authorizations_state(login_browser):
    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/authorizations", accept="text/html").text)
    require_testid(document, "empty-authorizations")
    assert_absent(document, "authorization-item-a_1")


def test_authorization_hidden_from_uninvolved_user(login_browser, api, reset, seed):
    tokens = seed(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    browser, _ = login_browser("cy")
    document = parse_html(browser.get("/authorizations", accept="text/html").text)
    assert_absent(document, "authorization-item-a_1")
    require_testid(document, "empty-authorizations")
    assert authorizations_json(api, tokens["cy"]) == []


# --------------------------------------------------------------------------- #
# Closed states
# --------------------------------------------------------------------------- #


def test_seeded_captured_authorization_renders_captured_amount(login_browser, api, reset, seed):
    tokens = seed(
        make_fixture(
            authorizations=[
                seeded_authorization("a_1", "u_ada", "u_bob", 2000, status="captured")
            ]
        )
    )
    expected = find_authorization(api, tokens["ada"], "a_1")
    assert expected["status"] == "captured"
    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/authorizations", accept="text/html").text)
    assert require_testid(document, "authorization-item-a_1").attrs.get("data-status") == "captured"
    assert captured_text(document, "a_1") == money_text(expected["captured_amount"], 2, "EUR")
    assert_absent(document, "authorization-capture-a_1")
    assert_absent(document, "authorization-void-a_1")


def test_seeded_voided_authorization_renders_without_controls(login_browser, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_1", "u_ada", "u_bob", 2000, status="voided")
            ]
        )
    )
    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/authorizations", accept="text/html").text)
    assert require_testid(document, "authorization-item-a_1").attrs.get("data-status") == "voided"
    assert_absent(document, "authorization-capture-a_1")
    assert_absent(document, "authorization-void-a_1")


def test_seeded_expired_authorization_renders_expired_and_releases_funds(login_browser, api, reset, seed):
    tokens = seed(
        make_fixture(
            authorizations=[
                seeded_authorization(
                    "a_1", "u_ada", "u_bob", 2000, status="open", expires_at=LONG_AGO
                )
            ]
        )
    )
    account = me(api, tokens["ada"])
    assert account["held"] == 0 and account["available"] == 10000
    assert [a["status"] for a in authorizations_json(api, tokens["ada"], status="expired")] == [
        "expired"
    ]
    assert authorizations_json(api, tokens["ada"], status="open") == []

    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/authorizations", accept="text/html").text)
    assert require_testid(document, "authorization-item-a_1").attrs.get("data-status") == "expired"
    wallet = parse_html(browser.get("/", accept="text/html").text)
    assert testid_attr(wallet, "wallet-available", "data-amount") == "10000"
    assert_absent(wallet, "wallet-held")
    assert_absent(document, "authorization-capture-a_1")
    assert_absent(document, "authorization-void-a_1")
