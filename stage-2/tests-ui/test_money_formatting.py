"""Exact ``wallet-*`` formatting and ``data-amount`` for every minor-unit scale."""

from playwright.sync_api import expect

from support import iso, make_fixture, seeded_authorization, user


def test_eur_balance_exact_text_and_data_amount(signed_in):
    page = signed_in("ada")
    balance = page.get_by_test_id("wallet-balance")
    expect(balance).to_have_text("100.00 EUR")
    assert balance.get_attribute("data-amount") == "10000"


def test_jpy_zero_decimals_has_no_decimal_point(signed_in, reset):
    reset(
        make_fixture(
            users=[user("u_ada", "ada", 1200), user("u_bob", "bob", 0), user("u_cy", "cy", 0)],
            currency="JPY",
            minor_units=0,
        )
    )
    page = signed_in("ada")
    balance = page.get_by_test_id("wallet-balance")
    expect(balance).to_have_text("1200 JPY")
    assert "." not in balance.inner_text()
    assert balance.get_attribute("data-amount") == "1200"


def test_bhd_three_decimals(signed_in, reset):
    reset(
        make_fixture(
            users=[user("u_ada", "ada", 1500), user("u_bob", "bob", 0), user("u_cy", "cy", 0)],
            currency="BHD",
            minor_units=3,
        )
    )
    page = signed_in("ada")
    balance = page.get_by_test_id("wallet-balance")
    expect(balance).to_have_text("1.500 BHD")
    assert balance.get_attribute("data-amount") == "1500"


def test_available_and_held_are_formatted_with_data_amount(signed_in, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_hold", "u_ada", "u_bob", 2000, expires_at=iso(7200))
            ]
        )
    )
    page = signed_in("ada")
    available = page.get_by_test_id("wallet-available")
    held = page.get_by_test_id("wallet-held")
    expect(available).to_have_text("80.00 EUR")
    assert available.get_attribute("data-amount") == "8000"
    expect(held).to_have_text("20.00 EUR")
    assert held.get_attribute("data-amount") == "2000"


def test_wallet_held_absent_when_zero(signed_in):
    page = signed_in("ada")
    expect(page.get_by_test_id("wallet-held")).to_have_count(0)
    expect(page.get_by_test_id("wallet-available")).to_have_text("100.00 EUR")


def test_available_is_the_spendable_headline_present_on_wallet(signed_in):
    page = signed_in("ada")
    expect(page.get_by_test_id("wallet-available")).to_be_visible()
    expect(page.get_by_test_id("wallet-balance")).to_be_visible()
