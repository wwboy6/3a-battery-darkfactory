"""The request form on ``/``."""

import pytest
from playwright.sync_api import expect

from support import fill_request_form, path_of, payload


def _is_request_post(request) -> bool:
    return request.method == "POST" and path_of(request.url) == "/requests"


def test_request_creates_a_pending_outgoing_request(signed_in):
    page = signed_in("ada")
    fill_request_form(page, "bob", "12.00", "taxi")
    with page.expect_response(_is_request_post) as info:
        page.get_by_test_id("request-submit").click()
    response = info.value
    assert response.status == 201, response.text()
    request_id = payload(response)["request_id"]

    page.goto("/requests")
    item = page.get_by_test_id(f"request-item-{request_id}")
    expect(item).to_have_attribute("data-status", "pending")
    expect(page.get_by_test_id(f"request-amount-{request_id}")).to_have_text("12.00 EUR")
    expect(page.get_by_test_id(f"request-cancel-{request_id}")).to_be_visible()


@pytest.mark.parametrize("typed,expected_minor", [("15", 1500), ("15.5", 1550), ("0.01", 1)])
def test_request_decimal_amounts_submit_minor_units(signed_in, typed, expected_minor):
    page = signed_in("ada")
    fill_request_form(page, "bob", typed, "taxi")
    with page.expect_response(_is_request_post) as info:
        page.get_by_test_id("request-submit").click()
    assert payload(info.value)["amount"] == expected_minor


@pytest.mark.parametrize("typed", ["15.005", "abc", "-1"])
def test_invalid_request_amount_is_rejected_without_a_request(signed_in, typed):
    page = signed_in("ada")
    posted = []
    page.on("request", lambda request: posted.append(request) if _is_request_post(request) else None)
    fill_request_form(page, "bob", typed, "taxi")
    page.get_by_test_id("request-submit").click()
    expect(page.get_by_test_id("request-error")).to_be_visible()
    page.wait_for_timeout(300)
    assert posted == []


def test_self_request_shows_error(signed_in):
    page = signed_in("ada")
    fill_request_form(page, "ada", "1.00")
    page.get_by_test_id("request-submit").click()
    expect(page.get_by_test_id("request-error")).to_be_visible()


def test_unknown_handle_shows_error(signed_in):
    page = signed_in("ada")
    fill_request_form(page, "nobody", "1.00")
    page.get_by_test_id("request-submit").click()
    expect(page.get_by_test_id("request-error")).to_be_visible()


def test_request_error_absent_initially(signed_in):
    page = signed_in("ada")
    expect(page.get_by_test_id("request-error")).to_have_count(0)
