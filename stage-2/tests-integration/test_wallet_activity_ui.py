"""Wallet numbers, money formatting, pay/request flows and the activity feed.

Each test seeds deterministic state, drives the JSON API the way ``app.js``
would, then reads the server-rendered HTML the browser would see and asserts
the two surfaces agree.
"""

from __future__ import annotations

import time

from support import (
    assert_absent,
    expect,
    make_fixture,
    me,
    parse_html,
    payload,
    require_testid,
    seeded_authorization,
    seeded_payment,
    seeded_request,
    testid_text,
    testids_in_order,
    unique,
    user,
)


def wallet_amount(document, testid="wallet-balance"):
    node = require_testid(document, testid)
    raw = node.attrs.get("data-amount")
    assert raw is not None, f"{testid} must carry data-amount"
    return int(raw)


def make_payment(api, token, to_handle, amount, *, key=None, note="", visibility="public"):
    return api.post(
        "/payments",
        token=token,
        idem=key or unique("pay"),
        body={"to_handle": to_handle, "amount": amount, "note": note, "visibility": visibility},
    )


# --------------------------------------------------------------------------- #
# Wallet numbers vs GET /me
# --------------------------------------------------------------------------- #


def test_wallet_numbers_match_api_with_no_holds(login_browser, api, ada_token):
    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/", accept="text/html").text)
    account = me(api, ada_token)

    assert wallet_amount(document) == account["total"] == 10000
    assert testid_text(document, "wallet-balance") == "100.00 EUR"
    assert wallet_amount(document, "wallet-available") == account["available"] == 10000
    assert testid_text(document, "wallet-available") == "100.00 EUR"
    assert_absent(document, "wallet-held")


def test_wallet_numbers_include_seeded_hold(login_browser, api, reset, seed):
    tokens = seed(
        make_fixture(
            authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]
        )
    )
    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/", accept="text/html").text)
    account = me(api, tokens["ada"])

    assert account["total"] == 10000 and account["held"] == 2000 and account["available"] == 8000
    assert wallet_amount(document) == 10000
    assert wallet_amount(document, "wallet-available") == 8000
    assert wallet_amount(document, "wallet-held") == 2000
    assert testid_text(document, "wallet-balance") == "100.00 EUR"
    assert testid_text(document, "wallet-available") == "80.00 EUR"
    assert testid_text(document, "wallet-held") == "20.00 EUR"


def test_money_formatting_with_zero_minor_units(login_browser, api, reset):
    reset(
        make_fixture(
            users=[user("u_ada", "ada", 1200), user("u_bob", "bob", 500)],
            currency="JPY",
            minor_units=0,
        )
    )
    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/", accept="text/html").text)
    balance = testid_text(document, "wallet-balance")
    assert balance == "1200 JPY"
    assert "." not in balance
    assert wallet_amount(document) == 1200
    assert testid_text(document, "wallet-available") == "1200 JPY"


def test_wallet_available_is_derived_not_negative(login_browser, api, reset, seed):
    # A hold larger than the wallet is a reset error, so the API never exposes a
    # negative available amount. With a 9000 hold on a 10000 wallet, 1000 remains.
    tokens = seed(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 9000)]))
    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/", accept="text/html").text)
    assert wallet_amount(document, "wallet-available") == 1000
    assert me(api, tokens["ada"])["available"] == 1000


# --------------------------------------------------------------------------- #
# Pay flow
# --------------------------------------------------------------------------- #


def test_pay_via_api_updates_wallet_and_feed_in_ui(login_browser, api, ada_token):
    browser, _ = login_browser("ada")
    resp = make_payment(api, ada_token, "bob", 1500, note="dinner")
    expect(resp, 201)
    payment = payload(resp)
    pid = payment["payment_id"]

    document = parse_html(browser.get("/", accept="text/html").text)
    assert wallet_amount(document) == 8500
    assert testid_text(document, "wallet-balance") == "85.00 EUR"
    item = require_testid(document, f"activity-item-{pid}")
    assert item.attrs.get("data-visibility") == "public"
    assert testid_text(document, f"activity-amount-{pid}") == "15.00 EUR"
    parties = testid_text(document, f"activity-parties-{pid}")
    assert "ada" in parties and "bob" in parties


def test_pay_receiver_sees_the_payment_and_new_balance(login_browser, api, ada_token):
    make_payment(api, ada_token, "bob", 500, note="coffee")
    browser, _ = login_browser("bob")
    document = parse_html(browser.get("/", accept="text/html").text)
    assert wallet_amount(document) == 3000
    assert len(testids_in_order(document)) > 0


def test_available_based_insufficient_funds_negative(login_browser, api, reset, seed):
    tokens = seed(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    browser, _ = login_browser("ada")

    # total is 10000 but only 8000 is available, so 8500 is refused.
    refused = make_payment(api, tokens["ada"], "bob", 8500)
    expect(refused, 409, "insufficient_funds")

    document = parse_html(browser.get("/", accept="text/html").text)
    assert wallet_amount(document, "wallet-available") == 8000
    assert wallet_amount(document, "wallet-balance") == 10000
    assert not has_any_activity(document)

    # Exactly the available amount is accepted.
    ok = make_payment(api, tokens["ada"], "bob", 8000)
    expect(ok, 201)
    account = me(api, tokens["ada"])
    assert account["total"] == 2000 and account["held"] == 2000 and account["available"] == 0


def has_any_activity(document):
    return any(t.startswith("activity-item-") for t in testids_in_order(document))


# --------------------------------------------------------------------------- #
# Request flow
# --------------------------------------------------------------------------- #


def test_request_screen_reflects_incoming_and_outgoing_per_party(
    login_browser, api, ada_token, bob_token
):
    created = api.post(
        "/requests",
        token=bob_token,
        idem=unique("req"),
        body={"payer_handle": "ada", "amount": 1200, "note": "taxi"},
    )
    expect(created, 201)
    rid = payload(created)["request_id"]

    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/requests", accept="text/html").text)
    item = require_testid(document, f"request-item-{rid}")
    assert item.attrs.get("data-status") == "pending"
    assert testid_text(document, f"request-amount-{rid}") == "12.00 EUR"
    require_testid(document, f"request-pay-{rid}")
    require_testid(document, f"request-decline-{rid}")
    assert_absent(document, f"request-cancel-{rid}")


def test_request_creator_sees_cancel_not_pay(login_browser, api, reset, seed):
    tokens = seed(make_fixture(request_list=[seeded_request("rq_1", "u_bob", "u_ada", 1200)]))
    browser, _ = login_browser("bob")
    document = parse_html(browser.get("/requests", accept="text/html").text)
    require_testid(document, "request-item-rq_1")
    require_testid(document, "request-cancel-rq_1")
    assert_absent(document, "request-pay-rq_1")
    assert_absent(document, "request-decline-rq_1")


def test_paying_request_via_api_updates_screen_and_balances(login_browser, api, reset, seed):
    tokens = seed(make_fixture(request_list=[seeded_request("rq_1", "u_bob", "u_ada", 1200, "taxi")]))
    pay = api.post(
        "/requests/rq_1/pay",
        token=tokens["ada"],
        idem=unique("rqpay"),
        body={"visibility": "private"},
    )
    expect(pay, 201)
    payment = payload(pay)
    pid = payment["payment_id"]
    assert payment["request_id"] == "rq_1"

    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/requests", accept="text/html").text)
    item = require_testid(document, "request-item-rq_1")
    assert item.attrs.get("data-status") == "paid"
    assert_absent(document, "request-pay-rq_1")
    assert_absent(document, "request-decline-rq_1")

    wallet = parse_html(browser.get("/", accept="text/html").text)
    assert testid_text(wallet, f"activity-amount-{pid}") == "12.00 EUR"

    assert me(api, tokens["ada"])["total"] == 8800
    assert me(api, tokens["bob"])["total"] == 3700


# --------------------------------------------------------------------------- #
# Activity feed contract
# --------------------------------------------------------------------------- #


def test_activity_visibility_rules_in_ui(login_browser, reset):
    reset(
        make_fixture(
            payments=[
                seeded_payment("p_pub", "u_ada", "u_bob", 100, "public one", "public"),
                seeded_payment("p_priv", "u_ada", "u_bob", 200, "private one", "private"),
            ]
        )
    )

    browser, _ = login_browser("cy")  # uninvolved third party
    document = parse_html(browser.get("/", accept="text/html").text)
    require_testid(document, "activity-item-p_pub")
    assert_absent(document, "activity-item-p_priv")

    browser, _ = login_browser("ada")  # sender sees both
    document = parse_html(browser.get("/", accept="text/html").text)
    require_testid(document, "activity-item-p_pub")
    priv = require_testid(document, "activity-item-p_priv")
    assert priv.attrs.get("data-visibility") == "private"

    browser, _ = login_browser("bob")  # receiver sees the private payment too
    document = parse_html(browser.get("/", accept="text/html").text)
    require_testid(document, "activity-item-p_priv")


def test_activity_feed_is_newest_first_in_dom(login_browser, api, ada_token):
    first = payload(expect(make_payment(api, ada_token, "bob", 100, note="first"), 201))
    time.sleep(1.1)  # distinct created_at seconds: ordering within a second is unspecified
    second = payload(expect(make_payment(api, ada_token, "bob", 200, note="second"), 201))

    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/", accept="text/html").text)
    order = [
        t for t in testids_in_order(document) if t.startswith("activity-item-")
    ]
    assert order.index(f"activity-item-{second['payment_id']}") < order.index(
        f"activity-item-{first['payment_id']}"
    )
    assert testid_text(document, f"activity-amount-{second['payment_id']}") == "2.00 EUR"


def test_activity_note_round_trips_unicode(login_browser, api, ada_token):
    note = "café ☕ 你好"
    payment = payload(expect(make_payment(api, ada_token, "bob", 100, note=note), 201))
    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/", accept="text/html").text)
    assert note in testid_text(document, f"activity-note-{payment['payment_id']}")


def test_request_cancelled_elsewhere_refreshes_to_terminal_state(login_browser, api, reset, seed):
    tokens = seed(make_fixture(request_list=[seeded_request("rq_1", "u_bob", "u_ada", 1200)]))
    browser, _ = login_browser("ada")
    document = parse_html(browser.get("/requests", accept="text/html").text)
    require_testid(document, "request-pay-rq_1")

    # The requester cancels from another client while this browser shows the button.
    expect(api.post("/requests/rq_1/cancel", token=tokens["bob"]), 200)
    document = parse_html(browser.get("/requests", accept="text/html").text)
    assert require_testid(document, "request-item-rq_1").attrs.get("data-status") == "cancelled"
    assert_absent(document, "request-pay-rq_1")
    assert_absent(document, "request-decline-rq_1")

    # Paying the stale request is refused and moves nothing.
    refused = api.post("/requests/rq_1/pay", token=tokens["ada"], idem=unique("stale"), body={})
    expect(refused, 409, "request_not_pending")
    assert me(api, tokens["ada"])["total"] == 10000
