"""The requests screen: lists, amounts and per-state action buttons."""

from playwright.sync_api import expect

from support import api_login, make_fixture, seeded_request

REQUESTS = [
    seeded_request("rq_in", "u_bob", "u_ada", 1200, "taxi", "pending"),
    seeded_request("rq_out", "u_ada", "u_bob", 800, "movie", "pending"),
    seeded_request("rq_paid", "u_bob", "u_ada", 500, "lunch", "paid"),
    seeded_request("rq_declined", "u_bob", "u_ada", 400, "old", "declined"),
    seeded_request("rq_cancelled", "u_ada", "u_cy", 300, "gone", "cancelled"),
]


def _seed_requests(reset):
    reset(make_fixture(requests_=REQUESTS))


def test_incoming_and_outgoing_lists(signed_in, reset):
    _seed_requests(reset)
    page = signed_in("ada")
    page.goto("/requests")
    incoming = page.get_by_test_id("incoming-list")
    outgoing = page.get_by_test_id("outgoing-list")
    expect(incoming.get_by_test_id("request-item-rq_in")).to_be_visible()
    expect(incoming.get_by_test_id("request-item-rq_paid")).to_be_visible()
    expect(outgoing.get_by_test_id("request-item-rq_out")).to_be_visible()
    expect(outgoing.get_by_test_id("request-item-rq_cancelled")).to_be_visible()
    expect(outgoing.get_by_test_id("request-item-rq_in")).to_have_count(0)


def test_request_amount_is_formatted(signed_in, reset):
    _seed_requests(reset)
    page = signed_in("ada")
    page.goto("/requests")
    expect(page.get_by_test_id("request-amount-rq_in")).to_have_text("12.00 EUR")
    expect(page.get_by_test_id("request-amount-rq_out")).to_have_text("8.00 EUR")


def test_pending_incoming_offers_pay_and_decline_only(signed_in, reset):
    _seed_requests(reset)
    page = signed_in("ada")
    page.goto("/requests")
    expect(page.get_by_test_id("request-pay-rq_in")).to_be_visible()
    expect(page.get_by_test_id("request-decline-rq_in")).to_be_visible()
    expect(page.get_by_test_id("request-cancel-rq_in")).to_have_count(0)


def test_pending_outgoing_offers_cancel_only(signed_in, reset):
    _seed_requests(reset)
    page = signed_in("ada")
    page.goto("/requests")
    expect(page.get_by_test_id("request-cancel-rq_out")).to_be_visible()
    expect(page.get_by_test_id("request-pay-rq_out")).to_have_count(0)
    expect(page.get_by_test_id("request-decline-rq_out")).to_have_count(0)


def test_non_pending_requests_have_no_action_buttons(signed_in, reset):
    _seed_requests(reset)
    page = signed_in("ada")
    page.goto("/requests")
    expect(page.get_by_test_id("request-item-rq_paid")).to_have_attribute("data-status", "paid")
    expect(page.get_by_test_id("request-item-rq_declined")).to_have_attribute("data-status", "declined")
    expect(page.get_by_test_id("request-item-rq_cancelled")).to_have_attribute("data-status", "cancelled")
    for request_id in ("rq_paid", "rq_declined", "rq_cancelled"):
        expect(page.get_by_test_id(f"request-pay-{request_id}")).to_have_count(0)
        expect(page.get_by_test_id(f"request-decline-{request_id}")).to_have_count(0)
        expect(page.get_by_test_id(f"request-cancel-{request_id}")).to_have_count(0)


def test_empty_requests_state(signed_in):
    page = signed_in("ada")
    page.goto("/requests")
    expect(page.get_by_test_id("empty-requests")).to_be_visible()


def test_decline_updates_state_and_buttons(signed_in, reset):
    _seed_requests(reset)
    page = signed_in("ada")
    page.goto("/requests")
    page.get_by_test_id("request-decline-rq_in").click()
    expect(page.get_by_test_id("request-item-rq_in")).to_have_attribute("data-status", "declined")
    expect(page.get_by_test_id("request-pay-rq_in")).to_have_count(0)
    expect(page.get_by_test_id("request-decline-rq_in")).to_have_count(0)


def test_cancel_updates_state_and_buttons(signed_in, reset):
    _seed_requests(reset)
    page = signed_in("ada")
    page.goto("/requests")
    page.get_by_test_id("request-cancel-rq_out").click()
    expect(page.get_by_test_id("request-item-rq_out")).to_have_attribute("data-status", "cancelled")
    expect(page.get_by_test_id("request-cancel-rq_out")).to_have_count(0)


def test_paying_a_request_cancelled_elsewhere_shows_error_and_refreshes(signed_in, reset, api):
    _seed_requests(reset)
    page = signed_in("ada")
    page.goto("/requests")
    expect(page.get_by_test_id("request-pay-rq_in")).to_be_visible()

    bob = api_login(api, "bob")
    cancel = api.post("/requests/rq_in/cancel", token=bob)
    assert cancel.status_code == 200, cancel.text

    page.get_by_test_id("request-pay-rq_in").click()
    expect(page.get_by_test_id("request-error")).to_be_visible()
    expect(page.get_by_test_id("request-pay-rq_in")).to_have_count(0)


def test_cancelling_a_request_changed_elsewhere_shows_error(signed_in, reset, api):
    _seed_requests(reset)
    page = signed_in("ada")
    page.goto("/requests")
    expect(page.get_by_test_id("request-cancel-rq_out")).to_be_visible()

    bob = api_login(api, "bob")
    decline = api.post("/requests/rq_out/decline", token=bob)
    assert decline.status_code == 200, decline.text

    page.get_by_test_id("request-cancel-rq_out").click()
    expect(page.get_by_test_id("request-error")).to_be_visible()
