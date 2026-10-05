"""``/authorizations`` screen plus the wallet's available/held numbers."""

import time

from playwright.sync_api import expect

from support import (
    api_login,
    ensure_authorize_form,
    fill_authorize_form,
    iso,
    make_fixture,
    parse_decimal_to_minor,
    path_of,
    payload,
    seeded_authorization,
    testid_attribute_order,
    unique,
)


def _is_authorization_post(request) -> bool:
    return request.method == "POST" and path_of(request.url) == "/authorizations"


def _holds():
    expiries = {"a_in": iso(3600), "a_out": iso(3600), "a_exp": iso(-3600)}
    fixture = make_fixture(
        authorizations=[
            seeded_authorization(
                "a_in", "u_bob", "u_ada", 2000, note="deposit", visibility="private",
                expires_at=expiries["a_in"],
            ),
            seeded_authorization("a_out", "u_ada", "u_bob", 3000, expires_at=expiries["a_out"]),
            seeded_authorization("a_exp", "u_ada", "u_bob", 1000, expires_at=expiries["a_exp"]),
        ]
    )
    return fixture, expiries


def _seed_holds(reset):
    fixture, expiries = _holds()
    reset(fixture)
    return expiries


def test_wallet_available_and_held_reflect_open_holds(signed_in, reset):
    _seed_holds(reset)
    page = signed_in("ada")
    expect(page.get_by_test_id("wallet-balance")).to_have_text("100.00 EUR")
    expect(page.get_by_test_id("wallet-available")).to_have_text("70.00 EUR")
    expect(page.get_by_test_id("wallet-held")).to_have_text("30.00 EUR")


def test_authorization_list_statuses_and_buttons_per_state(signed_in, reset):
    _seed_holds(reset)
    page = signed_in("ada")
    page.goto("/authorizations")
    expect(page.get_by_test_id("authorization-list").get_by_test_id("authorization-item-a_in")).to_be_visible()
    expect(page.get_by_test_id("authorization-list").get_by_test_id("authorization-item-a_out")).to_be_visible()

    expect(page.get_by_test_id("authorization-item-a_in")).to_have_attribute("data-status", "open")
    expect(page.get_by_test_id("authorization-item-a_out")).to_have_attribute("data-status", "open")
    expect(page.get_by_test_id("authorization-item-a_exp")).to_have_attribute("data-status", "expired")

    # Incoming open: capturable, not voidable.
    expect(page.get_by_test_id("authorization-capture-a_in")).to_be_visible()
    expect(page.get_by_test_id("authorization-capture-amount-a_in")).to_be_visible()
    expect(page.get_by_test_id("authorization-void-a_in")).to_have_count(0)

    # Outgoing open: voidable, not capturable.
    expect(page.get_by_test_id("authorization-void-a_out")).to_be_visible()
    expect(page.get_by_test_id("authorization-capture-a_out")).to_have_count(0)
    expect(page.get_by_test_id("authorization-capture-amount-a_out")).to_have_count(0)

    # Expired (clock) open seed: neither action.
    expect(page.get_by_test_id("authorization-void-a_exp")).to_have_count(0)
    expect(page.get_by_test_id("authorization-capture-a_exp")).to_have_count(0)

    # captured amount only appears for a captured authorisation.
    expect(page.get_by_test_id("authorization-captured-a_in")).to_have_count(0)
    expect(page.get_by_test_id("authorization-captured-a_exp")).to_have_count(0)


def test_authorization_amount_expiry_and_capture_prefill(signed_in, reset):
    expiries = _seed_holds(reset)
    page = signed_in("ada")
    page.goto("/authorizations")
    expect(page.get_by_test_id("authorization-amount-a_in")).to_have_text("20.00 EUR")
    expect(page.get_by_test_id("authorization-amount-a_out")).to_have_text("30.00 EUR")
    expect(page.get_by_test_id("authorization-expires-a_in")).to_have_text(expiries["a_in"])
    prefill = page.get_by_test_id("authorization-capture-amount-a_in").input_value()
    assert parse_decimal_to_minor(prefill, 2) == 2000


def test_expired_hold_is_not_counted(signed_in, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_exp", "u_ada", "u_bob", 5000, expires_at=iso(-3600))
            ]
        )
    )
    page = signed_in("ada")
    expect(page.get_by_test_id("wallet-available")).to_have_text("100.00 EUR")
    expect(page.get_by_test_id("wallet-held")).to_have_count(0)


def test_empty_authorizations_state(signed_in):
    page = signed_in("ada")
    page.goto("/authorizations")
    expect(page.get_by_test_id("empty-authorizations")).to_be_visible()
    expect(page.locator('[data-testid^="authorization-item-"]')).to_have_count(0)


def test_authorize_form_and_visibility_options(signed_in):
    page = signed_in("ada")
    ensure_authorize_form(page)
    expect(page.get_by_test_id("authorize-handle")).to_be_visible()
    expect(page.get_by_test_id("authorize-amount")).to_be_visible()
    expect(page.get_by_test_id("authorize-note")).to_be_visible()
    values = page.get_by_test_id("authorize-visibility").locator("option").evaluate_all(
        "els => els.map(e => e.value)"
    )
    assert set(values) == {"public", "private"}


def test_authorize_invalid_decimal_shows_error_without_request(signed_in):
    page = signed_in("ada")
    ensure_authorize_form(page)
    posted = []
    page.on(
        "request",
        lambda request: posted.append(request) if _is_authorization_post(request) else None,
    )
    fill_authorize_form(page, "bob", "15.005")
    page.get_by_test_id("authorize-submit").click()
    expect(page.get_by_test_id("authorize-error")).to_be_visible()
    page.wait_for_timeout(300)
    assert posted == []


def test_authorize_checks_available_not_total(signed_in, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_hold", "u_ada", "u_bob", 9000, expires_at=iso(3600))
            ]
        )
    )
    page = signed_in("ada")
    ensure_authorize_form(page)
    fill_authorize_form(page, "bob", "20.00")
    with page.expect_response(_is_authorization_post) as info:
        page.get_by_test_id("authorize-submit").click()
    assert info.value.status == 409
    expect(page.get_by_test_id("authorize-error")).to_be_visible()


def test_authorize_creates_open_hold_and_updates_wallet(signed_in):
    page = signed_in("ada")
    ensure_authorize_form(page)
    fill_authorize_form(page, "bob", "12.00", "rent", visibility="private")
    with page.expect_response(_is_authorization_post) as info:
        page.get_by_test_id("authorize-submit").click()
    body = payload(info.value)
    assert body["status"] == "open"
    authorization_id = body["authorization_id"]

    page.goto("/authorizations")
    expect(page.get_by_test_id(f"authorization-item-{authorization_id}")).to_have_attribute(
        "data-status", "open"
    )
    expect(page.get_by_test_id(f"authorization-amount-{authorization_id}")).to_have_text("12.00 EUR")
    expect(page.get_by_test_id(f"authorization-expires-{authorization_id}")).to_be_visible()
    page.goto("/")
    expect(page.get_by_test_id("wallet-available")).to_have_text("88.00 EUR")
    expect(page.get_by_test_id("wallet-held")).to_have_text("12.00 EUR")


def _create_authorization(api, token, handle, amount, **extra):
    body = {"to_handle": handle, "amount": amount}
    body.update(extra)
    resp = api.post("/authorizations", token=token, idem=unique(), body=body)
    assert resp.status_code == 201, resp.text
    return payload(resp)


def test_incoming_capture_moves_money_and_closes_hold(signed_in, api):
    ada = api_login(api, "ada")
    authorization = _create_authorization(api, ada, "bob", 2000, note="deposit")
    authorization_id = authorization["authorization_id"]

    bob = signed_in("bob")
    bob.goto("/authorizations")
    expect(bob.get_by_test_id(f"authorization-item-{authorization_id}")).to_have_attribute(
        "data-status", "open"
    )
    prefill = bob.get_by_test_id(f"authorization-capture-amount-{authorization_id}").input_value()
    assert parse_decimal_to_minor(prefill, 2) == 2000
    bob.get_by_test_id(f"authorization-capture-{authorization_id}").click()
    expect(bob.get_by_test_id(f"authorization-item-{authorization_id}")).to_have_attribute(
        "data-status", "captured"
    )
    expect(bob.get_by_test_id(f"authorization-captured-{authorization_id}")).to_have_text("20.00 EUR")
    expect(bob.get_by_test_id(f"authorization-capture-{authorization_id}")).to_have_count(0)

    ada_page = signed_in("ada")
    expect(ada_page.get_by_test_id("wallet-balance")).to_have_text("80.00 EUR")
    expect(ada_page.get_by_test_id("wallet-available")).to_have_text("80.00 EUR")
    expect(ada_page.get_by_test_id("wallet-held")).to_have_count(0)


def test_partial_final_capture_releases_remainder(signed_in, api):
    ada = api_login(api, "ada")
    authorization = _create_authorization(api, ada, "bob", 2000)
    authorization_id = authorization["authorization_id"]

    bob = signed_in("bob")
    bob.goto("/authorizations")
    bob.get_by_test_id(f"authorization-capture-amount-{authorization_id}").fill("15.00")
    bob.get_by_test_id(f"authorization-capture-{authorization_id}").click()
    expect(bob.get_by_test_id(f"authorization-item-{authorization_id}")).to_have_attribute(
        "data-status", "captured"
    )
    expect(bob.get_by_test_id(f"authorization-captured-{authorization_id}")).to_have_text("15.00 EUR")

    ada_page = signed_in("ada")
    expect(ada_page.get_by_test_id("wallet-balance")).to_have_text("85.00 EUR")
    expect(ada_page.get_by_test_id("wallet-held")).to_have_count(0)


def test_capture_above_remaining_shows_error(signed_in, api):
    ada = api_login(api, "ada")
    authorization = _create_authorization(api, ada, "bob", 2000)
    authorization_id = authorization["authorization_id"]

    bob = signed_in("bob")
    bob.goto("/authorizations")
    bob.get_by_test_id(f"authorization-capture-amount-{authorization_id}").fill("25.00")
    bob.get_by_test_id(f"authorization-capture-{authorization_id}").click()
    expect(bob.get_by_test_id("authorization-error")).to_be_visible()


def test_outgoing_void_releases_hold(signed_in, api):
    ada = api_login(api, "ada")
    authorization = _create_authorization(api, ada, "bob", 2000)
    authorization_id = authorization["authorization_id"]

    page = signed_in("ada")
    page.goto("/authorizations")
    expect(page.get_by_test_id(f"authorization-void-{authorization_id}")).to_be_visible()
    page.get_by_test_id(f"authorization-void-{authorization_id}").click()
    expect(page.get_by_test_id(f"authorization-item-{authorization_id}")).to_have_attribute(
        "data-status", "voided"
    )
    expect(page.get_by_test_id(f"authorization-void-{authorization_id}")).to_have_count(0)
    expect(page.get_by_test_id(f"authorization-captured-{authorization_id}")).to_have_count(0)
    page.goto("/")
    expect(page.get_by_test_id("wallet-available")).to_have_text("100.00 EUR")
    expect(page.get_by_test_id("wallet-held")).to_have_count(0)


def test_authorization_list_is_newest_first(signed_in, api):
    ada = api_login(api, "ada")
    first = _create_authorization(api, ada, "bob", 1000, note="first")
    time.sleep(1.1)
    second = _create_authorization(api, ada, "bob", 2000, note="second")

    page = signed_in("ada")
    page.goto("/authorizations")
    order = testid_attribute_order(page, "authorization-item-")
    assert order[:2] == [
        f"authorization-item-{second['authorization_id']}",
        f"authorization-item-{first['authorization_id']}",
    ]
