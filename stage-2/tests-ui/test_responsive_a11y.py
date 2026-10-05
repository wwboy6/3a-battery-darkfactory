"""Responsive layout (375 px) and basic accessibility of the forms."""

import pytest
from playwright.sync_api import expect

from support import (
    accessible_name,
    ensure_authorize_form,
    horizontal_overflow,
    iso,
    make_fixture,
    seeded_authorization,
)

MOBILE = {"width": 375, "height": 812}
DESKTOP = {"width": 1280, "height": 900}

SIGNED_IN_ROUTES = ["/", "/requests", "/split", "/authorizations"]


@pytest.mark.parametrize("route", SIGNED_IN_ROUTES)
def test_no_horizontal_scroll_at_375px(signed_in, route):
    page = signed_in("ada", viewport=MOBILE)
    page.goto(route)
    assert horizontal_overflow(page) <= 1, f"horizontal scroll on {route} at 375px"


@pytest.mark.parametrize("route", ["/login", "/signup"])
def test_public_routes_no_horizontal_scroll_at_375px(make_page, route):
    page = make_page(MOBILE)
    page.goto(route)
    assert horizontal_overflow(page) <= 1, f"horizontal scroll on {route} at 375px"


def test_no_horizontal_scroll_with_holds_at_375px(signed_in, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_in", "u_bob", "u_ada", 2000, expires_at=iso(7200)),
                seeded_authorization("a_out", "u_ada", "u_bob", 3000, expires_at=iso(7200)),
            ]
        )
    )
    page = signed_in("ada", viewport=MOBILE)
    for route in ("/", "/authorizations"):
        page.goto(route)
        assert horizontal_overflow(page) <= 1, f"horizontal scroll on {route} with holds"


def test_no_horizontal_scroll_desktop(signed_in):
    page = signed_in("ada", viewport=DESKTOP)
    for route in SIGNED_IN_ROUTES:
        page.goto(route)
        assert horizontal_overflow(page) <= 1, f"horizontal scroll on {route} at desktop"


@pytest.mark.parametrize(
    "testid",
    ["pay-handle", "pay-amount", "pay-note", "pay-visibility",
     "request-handle", "request-amount", "request-note"],
)
def test_wallet_form_inputs_have_accessible_names(signed_in, testid):
    page = signed_in("ada")
    assert accessible_name(page, testid) != "", f"{testid} has no accessible name"


@pytest.mark.parametrize("testid", ["split-amount", "split-handles", "split-note"])
def test_split_inputs_have_accessible_names(signed_in, testid):
    page = signed_in("ada")
    page.goto("/split")
    assert accessible_name(page, testid) != "", f"{testid} has no accessible name"


@pytest.mark.parametrize(
    "testid", ["authorize-handle", "authorize-amount", "authorize-note", "authorize-visibility"]
)
def test_authorize_inputs_have_accessible_names(signed_in, testid):
    page = signed_in("ada")
    ensure_authorize_form(page)
    assert accessible_name(page, testid) != "", f"{testid} has no accessible name"


def test_auth_inputs_have_accessible_names(make_page):
    page = make_page()
    page.goto("/login")
    for testid in ("login-email", "login-password"):
        assert accessible_name(page, testid) != "", f"{testid} has no accessible name"
    page.goto("/signup")
    for testid in ("signup-email", "signup-password", "signup-display-name"):
        assert accessible_name(page, testid) != "", f"{testid} has no accessible name"


def test_focused_input_has_a_visible_indicator(signed_in):
    page = signed_in("ada")
    field = page.get_by_test_id("pay-amount")
    field.focus()
    active = page.evaluate(
        "() => document.activeElement && document.activeElement.getAttribute('data-testid')"
    )
    assert active == "pay-amount"
    style = field.evaluate(
        """el => {
            const s = getComputedStyle(el);
            return {outlineStyle: s.outlineStyle, outlineWidth: parseFloat(s.outlineWidth) || 0,
                    boxShadow: s.boxShadow};
        }"""
    )
    has_outline = style["outlineStyle"] not in ("none", "") and style["outlineWidth"] > 0
    has_shadow = style["boxShadow"] not in ("none", "")
    assert has_outline or has_shadow, f"no visible focus indicator: {style}"
