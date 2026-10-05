"""The activity feed on ``/``."""

import time

from playwright.sync_api import expect

from support import (
    api_login,
    dom_testid_order,
    fill_pay_form,
    make_fixture,
    seeded_payment,
    unique,
)


def test_feed_is_newest_first(signed_in, api):
    token = api_login(api, "ada")
    first = api.post(
        "/payments",
        token=token,
        idem=unique(),
        body={"to_handle": "bob", "amount": 500, "note": "first", "visibility": "public"},
    ).json()
    time.sleep(1.1)
    second = api.post(
        "/payments",
        token=token,
        idem=unique(),
        body={"to_handle": "bob", "amount": 700, "note": "second", "visibility": "public"},
    ).json()

    page = signed_in("ada")
    page.goto("/")
    order = dom_testid_order(page, "activity-item-")
    assert order == [f"activity-item-{second['payment_id']}", f"activity-item-{first['payment_id']}"]


def test_public_and_private_visibility(signed_in, reset):
    reset(
        make_fixture(
            payments=[
                seeded_payment("p_pub", "u_bob", "u_cy", 300, "public one", "public"),
                seeded_payment("p_priv", "u_bob", "u_cy", 400, "private one", "private"),
                seeded_payment("p_mine", "u_ada", "u_bob", 100, "my private", "private"),
            ]
        )
    )
    page = signed_in("ada")
    expect(page.get_by_test_id("activity-item-p_pub")).to_be_visible()
    expect(page.get_by_test_id("activity-item-p_pub")).to_have_attribute("data-visibility", "public")
    expect(page.get_by_test_id("activity-item-p_priv")).to_have_count(0)
    expect(page.get_by_test_id("activity-item-p_mine")).to_be_visible()
    expect(page.get_by_test_id("activity-item-p_mine")).to_have_attribute("data-visibility", "private")


def test_parties_amount_and_note_text(signed_in, reset):
    reset(make_fixture(payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, "coffee ☕", "public")]))
    page = signed_in("ada")
    parties = page.get_by_test_id("activity-parties-p_1")
    expect(parties).to_contain_text("ada")
    expect(parties).to_contain_text("bob")
    expect(page.get_by_test_id("activity-amount-p_1")).to_have_text("5.00 EUR")
    expect(page.get_by_test_id("activity-note-p_1")).to_have_text("coffee ☕")


def test_empty_note_is_present_and_exact(signed_in, reset):
    reset(make_fixture(payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, "", "public")]))
    page = signed_in("ada")
    expect(page.get_by_test_id("activity-note-p_1")).to_have_count(1)
    assert page.get_by_test_id("activity-note-p_1").inner_text() == ""


def test_empty_activity_state(signed_in):
    page = signed_in("ada")
    expect(page.get_by_test_id("empty-activity")).to_be_visible()
    expect(page.locator('[data-testid^="activity-item-"]')).to_have_count(0)


def test_activity_list_contains_the_items(signed_in, reset):
    reset(make_fixture(payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, "coffee", "public")]))
    page = signed_in("ada")
    activity = page.get_by_test_id("activity-list")
    expect(activity).to_be_visible()
    expect(activity.get_by_test_id("activity-item-p_1")).to_be_visible()


def test_new_payment_appears_in_feed_without_manual_reload(signed_in):
    page = signed_in("ada")
    expect(page.get_by_test_id("empty-activity")).to_be_visible()
    fill_pay_form(page, "bob", "5.00", "fresh")
    page.get_by_test_id("pay-submit").click()
    expect(page.locator('[data-testid^="activity-item-"]')).to_have_count(1)
    expect(page.get_by_test_id("empty-activity")).to_be_hidden()
