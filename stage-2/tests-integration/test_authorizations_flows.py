"""Happy-path lifecycle: authorize, capture (full, partial, non-final) and void.

The API moves the money; the HTML must agree at every step. These tests pin the
hold/available invariants and the exact capture semantics from the stage-2 spec.
"""

from __future__ import annotations

from datetime import datetime

from support import (
    assert_absent,
    expect,
    login,
    make_fixture,
    me,
    parse_html,
    payload,
    require_testid,
    seeded_authorization,
    testid_attr,
    testid_text,
    testids_in_order,
    unique,
    user,
)


def create_authorization(api, token, body, key=None):
    return api.post("/authorizations", token=token, idem=key or unique("auth"), body=body)


def capture(api, token, aid, body, key=None):
    return api.post(
        "/authorizations/%s/capture" % aid,
        token=token,
        idem=key or unique("cap"),
        body=body,
    )


def auth_json(api, token, aid):
    resp = api.get("/authorizations", token=token)
    expect(resp, 200)
    for auth in payload(resp)["authorizations"]:
        if auth["authorization_id"] == aid:
            return auth
    raise AssertionError(f"authorization {aid!r} not returned")


def seconds_between(start, end):
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


# --------------------------------------------------------------------------- #
# Creating holds
# --------------------------------------------------------------------------- #


def test_create_authorization_holds_funds_and_shows_in_ui(login_browser, api, ada_token):
    created = create_authorization(
        api,
        ada_token,
        {"to_handle": "bob", "amount": 2000, "note": "deposit", "visibility": "private"},
    )
    expect(created, 201)
    auth = payload(created)
    aid = auth["authorization_id"]
    assert auth["status"] == "open"
    assert auth["from_handle"] == "ada" and auth["to_handle"] == "bob"
    assert auth["captured_amount"] == 0
    assert auth["remaining_amount"] == 2000
    assert auth["payment_id"] is None and auth["payment_ids"] == []
    # Default lifetime is 600 seconds from creation.
    assert seconds_between(auth["created_at"], auth["expires_at"]) == 600

    browser, _ = login_browser("ada")
    wallet = parse_html(browser.get("/", accept="text/html").text)
    assert testid_attr(wallet, "wallet-available", "data-amount") == "8000"
    assert testid_attr(wallet, "wallet-held", "data-amount") == "2000"
    assert testid_attr(wallet, "wallet-balance", "data-amount") == "10000"

    listing = parse_html(browser.get("/authorizations", accept="text/html").text)
    assert require_testid(listing, "authorization-item-%s" % aid).attrs.get("data-status") == "open"
    assert testid_text(listing, "authorization-amount-%s" % aid) == "20.00 EUR"
    require_testid(listing, "authorization-void-%s" % aid)


def test_configured_ttl_is_used_for_new_holds(api, reset, seed):
    tokens = seed(make_fixture(authorization_ttl_seconds=120))
    auth = payload(expect(create_authorization(api, tokens["ada"], {"to_handle": "bob", "amount": 100}), 201))
    assert seconds_between(auth["created_at"], auth["expires_at"]) == 120


def test_open_authorization_is_not_a_feed_item(login_browser, api, ada_token):
    create_authorization(api, ada_token, {"to_handle": "bob", "amount": 2000})
    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/", accept="text/html").text)
    activity = [t for t in testids_in_order(document) if t.startswith("activity-item-")]
    assert activity == []
    feed = api.get("/activity", token=ada_token)
    expect(feed, 200)
    assert payload(feed)["payments"] == []



# --------------------------------------------------------------------------- #
# Capturing holds
# --------------------------------------------------------------------------- #


def test_default_capture_moves_all_and_closes_hold(login_browser, api, reset, seed):
    tokens = seed(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000, note="dep", visibility="private")]))
    captured = capture(api, tokens["bob"], "a_1", {})
    expect(captured, 201)
    payment = payload(captured)
    assert payment["authorization_id"] == "a_1"
    assert payment["request_id"] is None
    assert payment["amount"] == 2000
    assert payment["note"] == "dep" and payment["visibility"] == "private"

    assert me(api, tokens["ada"])["available"] == 10000 and me(api, tokens["ada"])["held"] == 0
    assert me(api, tokens["bob"])["total"] == 4500

    browser, _ = login_browser("bob")  # receiver
    listing = parse_html(browser.get("/authorizations", accept="text/html").text)
    assert require_testid(listing, "authorization-item-a_1").attrs.get("data-status") == "captured"
    assert testid_text(listing, "authorization-captured-a_1") == "20.00 EUR"
    assert_absent(listing, "authorization-capture-a_1")
    assert_absent(listing, "authorization-void-a_1")

    wallet = parse_html(browser.get("/", accept="text/html").text)
    require_testid(wallet, "activity-item-%s" % payment["payment_id"])


def test_partial_final_capture_releases_remainder(login_browser, api, reset, seed):
    tokens = seed(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    payment = payload(expect(capture(api, tokens["bob"], "a_1", {"amount": 1500}), 201))
    assert payment["amount"] == 1500
    assert me(api, tokens["ada"])["available"] == 8500 and me(api, tokens["ada"])["held"] == 0
    browser, _ = login_browser("ada")
    listing = parse_html(browser.get("/authorizations", accept="text/html").text)
    assert testid_text(listing, "authorization-captured-a_1") == "15.00 EUR"
    assert_absent(parse_html(browser.get("/", accept="text/html").text), "wallet-held")


def test_nonfinal_capture_keeps_remainder_held(login_browser, api, reset, seed):
    tokens = seed(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    first = payload(expect(capture(api, tokens["bob"], "a_1", {"amount": 700, "final": False}), 201))
    auth = auth_json(api, tokens["ada"], "a_1")
    assert auth["status"] == "open"
    assert auth["captured_amount"] == 700 and auth["remaining_amount"] == 1300
    assert auth["payment_ids"] == [first["payment_id"]]
    assert me(api, tokens["ada"])["available"] == 8700 and me(api, tokens["ada"])["held"] == 1300

    browser, _ = login_browser("bob")
    listing = parse_html(browser.get("/authorizations", accept="text/html").text)
    assert require_testid(listing, "authorization-item-a_1").attrs.get("data-status") == "open"
    assert testid_attr(listing, "authorization-capture-amount-a_1", "value") == "13.00"

    # Capturing the whole remaining amount closes it even with final=false.
    second = payload(expect(capture(api, tokens["bob"], "a_1", {"amount": 1300, "final": False}), 201))
    assert auth_json(api, tokens["ada"], "a_1")["status"] == "captured"
    assert me(api, tokens["ada"])["available"] == 10000 and me(api, tokens["ada"])["held"] == 0
    assert me(api, tokens["bob"])["total"] == 4500
    assert second["amount"] == 1300


def test_cumulative_captures_are_capped_at_authorized_amount(api, reset, seed):
    tokens = seed(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 1000)]))
    capture(api, tokens["bob"], "a_1", {"amount": 400, "final": False})
    capture(api, tokens["bob"], "a_1", {"amount": 400, "final": False})
    auth = auth_json(api, tokens["ada"], "a_1")
    assert auth["captured_amount"] == 800 and auth["remaining_amount"] == 200
    expect(capture(api, tokens["bob"], "a_1", {"amount": 300, "final": False}), 422, "capture_exceeds_authorization")
    assert me(api, tokens["ada"])["total"] == 9200  # 800 moved, 200 still held


def test_capture_response_reports_remaining_and_payment_ids(api, reset, seed):
    tokens = seed(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    p1 = payload(expect(capture(api, tokens["bob"], "a_1", {"amount": 600, "final": False}), 201))
    p2 = payload(expect(capture(api, tokens["bob"], "a_1", {"amount": 500, "final": False}), 201))
    auth = auth_json(api, tokens["ada"], "a_1")
    assert auth["captured_amount"] == 1100
    assert auth["remaining_amount"] == 900
    assert auth["payment_ids"] == [p1["payment_id"], p2["payment_id"]]
    assert auth["payment_id"] == p2["payment_id"]


def test_capture_with_zero_minor_units_shows_and_moves_jpy(login_browser, api, reset):
    reset(
        make_fixture(
            users=[user("u_ada", "ada", 1000), user("u_bob", "bob", 0)],
            currency="JPY",
            minor_units=0,
            authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 600)],
        )
    )
    browser, _ = login_browser("bob")
    document = parse_html(browser.get("/authorizations", accept="text/html").text)
    assert testid_text(document, "authorization-amount-a_1") == "600 JPY"
    assert testid_attr(document, "authorization-capture-amount-a_1", "value") == "600"

    bob_token = payload(expect(login(api, "bob@example.com"), 200))["token"]
    payment = payload(expect(capture(api, bob_token, "a_1", {}), 201))
    assert payment["amount"] == 600
    assert me(api, bob_token)["total"] == 600


# --------------------------------------------------------------------------- #
# Voiding holds
# --------------------------------------------------------------------------- #


def test_void_by_payer_releases_hold_and_is_repeatable(login_browser, api, reset, seed):
    tokens = seed(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    first = api.post("/authorizations/a_1/void", token=tokens["ada"])
    expect(first, 200)
    assert payload(first)["status"] == "voided"
    assert me(api, tokens["ada"])["available"] == 10000 and me(api, tokens["ada"])["held"] == 0

    repeat = api.post("/authorizations/a_1/void", token=tokens["ada"])
    expect(repeat, 200)
    assert payload(repeat)["status"] == "voided"

    # A voided hold can never be captured.
    expect(capture(api, tokens["bob"], "a_1", {}), 409, "authorization_not_open")

    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/authorizations", accept="text/html").text)
    assert require_testid(document, "authorization-item-a_1").attrs.get("data-status") == "voided"
    assert_absent(document, "authorization-void-a_1")
    assert_absent(parse_html(browser.get("/", accept="text/html").text), "wallet-held")


def test_partially_captured_hold_can_be_voided_for_the_remainder(api, reset, seed):
    tokens = seed(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    capture(api, tokens["bob"], "a_1", {"amount": 800, "final": False})
    voided = api.post("/authorizations/a_1/void", token=tokens["ada"])
    expect(voided, 200)
    auth = auth_json(api, tokens["ada"], "a_1")
    assert auth["status"] == "voided"
    assert auth["captured_amount"] == 800  # capture records preserved
    assert me(api, tokens["ada"])["available"] == 9200 and me(api, tokens["ada"])["held"] == 0
    assert me(api, tokens["bob"])["total"] == 3300
