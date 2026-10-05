"""§6 Authentication: signup, handle derivation, login, tokens."""

import pytest

from support import DEFAULT_PASSWORD, expect, login, payload, signup


# --------------------------------------------------------------------------- #
# Signup — happy path
# --------------------------------------------------------------------------- #


def test_signup_returns_201_with_token_that_works_on_me(api):
    resp = signup(api, "grace@example.com", display_name="Grace")
    expect(resp, 201)
    body = payload(resp)
    assert set(body) >= {"user_id", "display_name", "token"}
    assert body["display_name"] == "Grace"
    assert isinstance(body["token"], str) and body["token"]

    me = api.get("/me", token=body["token"])
    expect(me, 200)
    assert payload(me)["user_id"] == body["user_id"]


def test_signup_derives_handle_from_email_local_part(api):
    cases = {
        "Ada.Lovelace@example.com": "ada_lovelace",
        "Bob+Tag@example.com": "bob_tag",
        "UPPER@example.com": "upper",
        "weird!chars#here@example.com": "weird_chars_here",
    }
    for index, (email, handle) in enumerate(cases.items()):
        resp = signup(api, email)
        expect(resp, 201)
        token = payload(resp)["token"]
        me = api.get("/me", token=token)
        expect(me, 200)
        assert payload(me)["handle"] == handle, email


def test_signup_truncates_derived_handle_to_20_chars(api):
    local = "abcdefghijklmnopqrstuvwxyz"  # 26 chars
    resp = signup(api, f"{local}@example.com")
    expect(resp, 201)
    token = payload(resp)["token"]
    me = api.get("/me", token=token)
    assert payload(me)["handle"] == "abcdefghijklmnopqrst"  # first 20


def test_signup_new_user_starts_with_zero_balance(api):
    resp = signup(api, "fresh@example.com")
    expect(resp, 201)
    token = payload(resp)["token"]
    me = api.get("/me", token=token)
    expect(me, 200)
    assert payload(me)["balance"] == 0


def test_signup_ignores_unknown_handle_field(api):
    """A `handle` in the signup body must not override the derived handle."""
    resp = signup(api, "sneaky@example.com", handle="chosen")
    expect(resp, 201)
    token = payload(resp)["token"]
    me = api.get("/me", token=token)
    expect(me, 200)
    assert payload(me)["handle"] == "sneaky"


# --------------------------------------------------------------------------- #
# Signup — negative cases
# --------------------------------------------------------------------------- #


def test_signup_email_already_registered_is_409_email_taken(api):
    expect(signup(api, "ada@example.com"), 409, "email_taken")


def test_signup_derived_handle_collision_is_409_handle_taken(api):
    """A *different* email deriving to `ada` collides with the seeded handle."""
    resp = signup(api, "ADA@other.example.com")
    expect(resp, 409, "handle_taken")


def test_signup_handle_taken_creates_no_account(api):
    expect(signup(api, "ADA@other.example.com"), 409, "handle_taken")
    # The email was never registered, so login must fail.
    expect(login(api, "ADA@other.example.com"), 401, "unauthenticated")


@pytest.mark.parametrize("password", ["", "short", "1234567"])  # 0, 5, 7 chars
def test_signup_password_shorter_than_8_is_422(api, password):
    expect(signup(api, f"pw-{len(password)}-{password or 'empty'}@example.com", password), 422, "validation_failed")


def test_signup_password_of_exactly_8_is_accepted(api):
    expect(signup(api, "eight@example.com", "12345678"), 201)


@pytest.mark.parametrize(
    "email",
    ["noatsign", "a@", "@example.com", "space man@example.com", "a@@b.com"],
)
def test_signup_invalid_email_shape_is_422(api, email):
    expect(signup(api, email), 422, "validation_failed")


@pytest.mark.parametrize(
    "field,value",
    [("email", 123), ("password", 123), ("display_name", 42)],
)
def test_signup_wrong_json_type_is_400(api, field, value):
    body = {"email": "types@example.com", "password": DEFAULT_PASSWORD, "display_name": "T"}
    body[field] = value
    expect(api.post("/auth/signup", body=body), 400, "malformed_request")


@pytest.mark.parametrize("missing", ["email", "password", "display_name"])
def test_signup_missing_required_field_is_422(api, missing):
    body = {"email": "m@example.com", "password": DEFAULT_PASSWORD, "display_name": "M"}
    del body[missing]
    expect(api.post("/auth/signup", body=body), 422, "validation_failed")


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #


def test_seeded_user_can_log_in_immediately(api):
    resp = login(api, "ada@example.com")
    expect(resp, 200)
    body = payload(resp)
    assert set(body) >= {"user_id", "display_name", "token"}
    assert body["display_name"] == "Ada"
    assert payload(api.get("/me", token=body["token"]))["handle"] == "ada"


def test_login_wrong_password_is_401(api):
    expect(login(api, "ada@example.com", "wrong password"), 401, "unauthenticated")


def test_login_unknown_email_is_401(api):
    expect(login(api, "nobody@example.com"), 401, "unauthenticated")


def test_login_wrong_json_type_is_400(api):
    expect(api.post("/auth/login", body={"email": 5, "password": "x"}), 400, "malformed_request")


def test_login_missing_required_field_is_422(api):
    expect(api.post("/auth/login", body={"email": "ada@example.com"}), 422, "validation_failed")


def test_multiple_concurrent_tokens_are_all_valid(api):
    first = login(api, "ada@example.com")
    second = login(api, "ada@example.com")
    expect(first, 200)
    expect(second, 200)
    t1, t2 = payload(first)["token"], payload(second)["token"]
    assert t1 != t2
    for token in (t1, t2):
        me = api.get("/me", token=token)
        expect(me, 200)
        assert payload(me)["handle"] == "ada"


# --------------------------------------------------------------------------- #
# Bearer token handling
# --------------------------------------------------------------------------- #


def test_me_without_token_is_401(api):
    expect(api.get("/me"), 401, "unauthenticated")


@pytest.mark.parametrize(
    "header",
    ["", "Bearer", "Bearer ", "Token abc", "abc", "Basic Zm9v"],
)
def test_malformed_authorization_header_is_401(api, header):
    expect(api.get("/me", headers={"Authorization": header}), 401, "unauthenticated")


def test_unknown_bearer_token_is_401(api):
    expect(api.get("/me", token="definitely-not-a-real-token"), 401, "unauthenticated")


def test_health_reset_and_auth_endpoints_need_no_token(api):
    expect(api.get("/health"), 200)
    # reset is exercised by the autouse fixture; assert it accepts no auth here too.
    expect(api.post("/_test/reset", body={"currency": "EUR", "minor_units": 2, "users": []}), 204)
