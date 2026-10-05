"""Competing clients: refresh ordering, uncertain outcomes and stale reads."""

import time

from playwright.sync_api import expect

from support import api_login, fill_pay_form, is_post_to, unique


def _is_payment_post(target) -> bool:
    return is_post_to(target, "/payments")


def test_wallet_refresh_latest_wins(signed_in, api):
    page = signed_in("ada")
    expect(page.get_by_test_id("wallet-balance")).to_have_text("100.00 EUR")

    state = {"hold": True}
    held = []

    def handler(route, request):
        if (
            state["hold"]
            and request.resource_type in ("document", "fetch", "xhr")
            and "/static/" not in request.url
            and path_of(request.url) != "/health"
        ):
            held.append(route)
        else:
            route.continue_()

    page.route("**/*", handler)

    # First refresh: hold its response so it can only arrive late.
    page.get_by_test_id("wallet-refresh").evaluate("el => el.click()")
    deadline = time.monotonic() + 5
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert held, "wallet-refresh did not issue a readable request"

    state["hold"] = False
    token = api_login(api, "ada")
    spend = api.post(
        "/payments", token=token, idem=unique(), body={"to_handle": "bob", "amount": 1000}
    )
    assert spend.status_code == 201, spend.text

    # Second refresh sees the new balance.
    page.get_by_test_id("wallet-refresh").evaluate("el => el.click()")
    expect(page.get_by_test_id("wallet-balance")).to_have_text("90.00 EUR")

    # Release the delayed first read; it must not overwrite the newer value.
    for route in held:
        try:
            route.continue_()
        except Exception:  # noqa: BLE001 - a superseded navigation may already be gone
            pass
    page.wait_for_timeout(500)
    expect(page.get_by_test_id("wallet-balance")).to_have_text("90.00 EUR")


def test_wallet_refresh_keeps_pay_form_values(signed_in):
    page = signed_in("ada")
    fill_pay_form(page, "bob", "15.00", "kept")
    page.get_by_test_id("wallet-refresh").click()
    expect(page.get_by_test_id("wallet-balance")).to_have_text("100.00 EUR")
    expect(page.get_by_test_id("pay-handle")).to_have_value("bob")
    expect(page.get_by_test_id("pay-amount")).to_have_value("15.00")
    expect(page.get_by_test_id("pay-note")).to_have_value("kept")


def test_lost_response_shows_uncertain_then_same_key_retry_moves_money_once(signed_in, api):
    page = signed_in("ada")
    attempts = {"count": 0}

    def handler(route, request):
        attempts["count"] += 1
        if attempts["count"] == 1:
            route.fetch()  # the server processes and commits the payment
            route.abort()  # ...but the page never sees the response
        else:
            route.continue_()

    page.route("**/payments", handler)
    fill_pay_form(page, "bob", "15.00", "lost")
    page.get_by_test_id("pay-submit").click()

    expect(page.get_by_test_id("pay-uncertain")).to_be_visible()
    assert page.get_by_test_id("pay-uncertain").inner_text().strip() != ""
    expect(page.get_by_test_id("pay-error")).to_have_count(0)

    # Retry with the unchanged form: same key and body must replay, not double-spend.
    page.get_by_test_id("pay-submit").click()
    expect(page.locator('[data-testid^="activity-item-"]')).to_have_count(1)
    expect(page.get_by_test_id("pay-uncertain")).to_have_count(0)
    expect(page.get_by_test_id("pay-error")).to_have_count(0)
    expect(page.get_by_test_id("wallet-balance")).to_have_text("85.00 EUR")

    token = api_login(api, "ada")
    payments = api.get("/activity", token=token).json()["payments"]
    assert len(payments) == 1, payments


def test_refusal_from_a_changed_wallet_shows_error_refreshes_and_preserves_inputs(signed_in, api):
    page = signed_in("ada")
    fill_pay_form(page, "bob", "15.00", "late")

    # Another client spends most of the balance after this page was rendered.
    token = api_login(api, "ada")
    spend = api.post(
        "/payments", token=token, idem=unique(), body={"to_handle": "bob", "amount": 9500}
    )
    assert spend.status_code == 201, spend.text

    with page.expect_response(_is_payment_post) as info:
        page.get_by_test_id("pay-submit").click()
    assert info.value.status == 409
    expect(page.get_by_test_id("pay-error")).to_be_visible()
    expect(page.get_by_test_id("pay-uncertain")).to_have_count(0)
    expect(page.get_by_test_id("pay-handle")).to_have_value("bob")
    expect(page.get_by_test_id("pay-amount")).to_have_value("15.00")
    expect(page.get_by_test_id("pay-note")).to_have_value("late")
    expect(page.get_by_test_id("wallet-balance")).to_have_text("5.00 EUR")
