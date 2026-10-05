"""The pay form: decimal parsing, client-side rejection, retention and replays."""

import pytest
from playwright.sync_api import expect

from support import (
    fill_pay_form,
    make_fixture,
    path_of,
    payload,
    user,
    wallet_text,
)


def _is_payment_post(request) -> bool:
    return request.method == "POST" and path_of(request.url) == "/payments"


@pytest.mark.parametrize(
    "typed,expected_minor",
    [("15", 1500), ("15.00", 1500), ("15.5", 1550), ("0.01", 1), ("1.10", 110)],
)
def test_decimal_amounts_submit_minor_units(signed_in, typed, expected_minor):
    page = signed_in("ada")
    fill_pay_form(page, "bob", typed, "lunch")
    with page.expect_response(_is_payment_post) as info:
        page.get_by_test_id("pay-submit").click()
    response = info.value
    assert response.status == 201, response.text()
    assert payload(response)["amount"] == expected_minor


@pytest.mark.parametrize("typed", ["15.005", "1.5.5", "abc", "-5", "15,5"])
def test_invalid_decimal_is_rejected_without_a_request(signed_in, typed):
    page = signed_in("ada")
    posted = []
    page.on("request", lambda request: posted.append(request) if _is_payment_post(request) else None)
    before = wallet_text(page)
    fill_pay_form(page, "bob", typed, "lunch")
    page.get_by_test_id("pay-submit").click()
    error = page.get_by_test_id("pay-error")
    expect(error).to_be_visible()
    assert error.inner_text().strip() != ""
    page.wait_for_timeout(300)
    assert posted == [], f"invalid amount {typed!r} must not be sent to the API"
    assert wallet_text(page) == before


def test_visibility_select_exposes_only_public_and_private(signed_in):
    page = signed_in("ada")
    values = page.get_by_test_id("pay-visibility").locator("option").evaluate_all(
        "els => els.map(e => e.value)"
    )
    assert set(values) == {"public", "private"}


def test_private_visibility_and_note_are_submitted(signed_in):
    page = signed_in("ada")
    fill_pay_form(page, "bob", "15.00", "dinner 🍕", visibility="private")
    with page.expect_response(_is_payment_post) as info:
        page.get_by_test_id("pay-submit").click()
    body = payload(info.value)
    assert body["visibility"] == "private"
    assert body["note"] == "dinner 🍕"


def test_default_note_and_visibility(signed_in):
    page = signed_in("ada")
    fill_pay_form(page, "bob", "15.00")
    with page.expect_response(_is_payment_post) as info:
        page.get_by_test_id("pay-submit").click()
    body = payload(info.value)
    assert body["note"] == ""
    assert body["visibility"] == "public"


def test_form_values_are_kept_after_success(signed_in):
    page = signed_in("ada")
    fill_pay_form(page, "bob", "15.00", "kept", visibility="private")
    with page.expect_response(_is_payment_post):
        page.get_by_test_id("pay-submit").click()
    expect(page.get_by_test_id("pay-handle")).to_have_value("bob")
    expect(page.get_by_test_id("pay-amount")).to_have_value("15.00")
    expect(page.get_by_test_id("pay-note")).to_have_value("kept")
    expect(page.get_by_test_id("pay-visibility")).to_have_value("private")


def test_resubmitting_unchanged_form_moves_money_once(signed_in):
    page = signed_in("ada")
    fill_pay_form(page, "bob", "15.00", "once")
    with page.expect_response(_is_payment_post):
        page.get_by_test_id("pay-submit").click()
    items = page.locator('[data-testid^="activity-item-"]')
    expect(items).to_have_count(1)
    balance_after_first = wallet_text(page)
    assert balance_after_first == "85.00 EUR"

    submit = page.get_by_test_id("pay-submit")
    if submit.is_enabled():
        submit.click()
    page.wait_for_timeout(400)
    expect(items).to_have_count(1)
    assert wallet_text(page) == balance_after_first
    expect(page.get_by_test_id("pay-error")).to_have_count(0)


def test_changing_a_field_starts_a_new_payment(signed_in):
    page = signed_in("ada")
    fill_pay_form(page, "bob", "15.00", "first")
    with page.expect_response(_is_payment_post):
        page.get_by_test_id("pay-submit").click()
    items = page.locator('[data-testid^="activity-item-"]')
    expect(items).to_have_count(1)

    page.get_by_test_id("pay-amount").fill("16.00")
    submit = page.get_by_test_id("pay-submit")
    expect(submit).to_be_enabled()
    with page.expect_response(_is_payment_post):
        submit.click()
    expect(items).to_have_count(2)
    assert wallet_text(page) == "69.00 EUR"


def test_insufficient_funds_shows_error_and_preserves_inputs(signed_in, reset):
    reset(
        make_fixture(
            users=[user("u_ada", "ada", 100), user("u_bob", "bob", 2500), user("u_cy", "cy", 0)]
        )
    )
    page = signed_in("ada")
    fill_pay_form(page, "bob", "15.00", "too much")
    with page.expect_response(_is_payment_post) as info:
        page.get_by_test_id("pay-submit").click()
    assert info.value.status == 409
    expect(page.get_by_test_id("pay-error")).to_be_visible()
    expect(page.get_by_test_id("pay-handle")).to_have_value("bob")
    expect(page.get_by_test_id("pay-amount")).to_have_value("15.00")
    expect(page.get_by_test_id("pay-note")).to_have_value("too much")
    assert wallet_text(page) == "1.00 EUR"


def test_self_payment_shows_error(signed_in):
    page = signed_in("ada")
    fill_pay_form(page, "ada", "1.00")
    page.get_by_test_id("pay-submit").click()
    expect(page.get_by_test_id("pay-error")).to_be_visible()


def test_unknown_handle_shows_error(signed_in):
    page = signed_in("ada")
    fill_pay_form(page, "does-not-exist", "1.00")
    page.get_by_test_id("pay-submit").click()
    expect(page.get_by_test_id("pay-error")).to_be_visible()
