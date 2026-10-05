"""Signup, login, logout and identity badges."""

from playwright.sync_api import expect

from support import login_ui, logout_ui, signup_ui

SIGNED_IN_ROUTES = ["/", "/requests", "/split", "/authorizations"]


def test_signup_creates_signed_in_session(page):
    signup_ui(page, "new.user@example.com", display_name="New Person")
    expect(page.get_by_test_id("current-user")).to_be_visible()
    expect(page.get_by_test_id("current-user")).to_contain_text("New Person")
    expect(page.get_by_test_id("login-submit")).to_have_count(0)


def test_signup_duplicate_email_shows_auth_error(page):
    signup_ui(page, "ada@example.com", display_name="Impostor")
    error = page.get_by_test_id("auth-error")
    expect(error).to_be_visible()
    assert error.inner_text().strip() != ""
    expect(page.get_by_test_id("current-user")).to_have_count(0)


def test_signup_bad_password_shows_auth_error(page):
    signup_ui(page, "another@example.com", password="x", display_name="Another")
    expect(page.get_by_test_id("auth-error")).to_be_visible()
    expect(page.get_by_test_id("current-user")).to_have_count(0)


def test_login_success_redirects_to_wallet(page):
    login_ui(page, "ada@example.com")
    expect(page.get_by_test_id("current-user")).to_be_visible()
    expect(page.get_by_test_id("current-user")).to_contain_text("Ada")
    expect(page.get_by_test_id("wallet-balance")).to_be_visible()


def test_login_wrong_password_shows_auth_error(page):
    page.goto("/login")
    page.get_by_test_id("login-email").fill("ada@example.com")
    page.get_by_test_id("login-password").fill("not the password")
    page.get_by_test_id("login-submit").click()
    expect(page.get_by_test_id("auth-error")).to_be_visible()
    expect(page.get_by_test_id("current-user")).to_have_count(0)


def test_auth_error_absent_on_fresh_login(page):
    page.goto("/login")
    expect(page.get_by_test_id("login-submit")).to_be_visible()
    expect(page.get_by_test_id("auth-error")).to_have_count(0)


def test_logout_clears_the_session(signed_in):
    page = signed_in("ada")
    logout_ui(page)
    expect(page.get_by_test_id("current-user")).to_have_count(0)
    expect(page.get_by_test_id("wallet-balance")).to_have_count(0)


def test_identity_badges_visible_on_every_route(signed_in):
    page = signed_in("ada")
    for route in SIGNED_IN_ROUTES:
        page.goto(route)
        expect(page.get_by_test_id("current-user")).to_be_visible()
        expect(page.get_by_test_id("current-user")).to_contain_text("Ada")
        expect(page.get_by_test_id("current-handle")).to_be_visible()
        expect(page.get_by_test_id("logout-button")).to_be_visible()


def test_current_handle_is_exactly_the_handle(signed_in):
    page = signed_in("bob")
    handle = page.get_by_test_id("current-handle")
    expect(handle).to_have_text("bob")
    assert "@" not in handle.inner_text()


def test_anonymous_cannot_see_the_wallet(page):
    page.goto("/")
    expect(page.get_by_test_id("current-user")).to_have_count(0)
    expect(page.get_by_test_id("wallet-balance")).to_have_count(0)
