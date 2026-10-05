"""Stage-2: lazy authorization expiry and TTL."""

from support import (
    authorize,
    capture,
    expect,
    login,
    make_fixture,
    me,
    payload,
    seeded_authorization,
    seconds_from_now,
    ttl_seconds,
    void_authorization,
)


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def find_auth(api, token, aid):
    items = payload(api.get("/authorizations", token=token))["authorizations"]
    return next(a for a in items if a["authorization_id"] == aid)


def test_seeded_past_expiry_holds_nothing(api, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_past", "u_ada", "u_bob", 2000, expires_at=seconds_from_now(-7200))
            ]
        )
    )
    tok = relogin(api, "ada", "bob")
    body = me(api, tok["ada"])
    assert body["held"] == 0 and body["available"] == 10000
    assert find_auth(api, tok["ada"], "a_past")["status"] == "expired"


def test_seeded_future_expiry_remains_open(api, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_future", "u_ada", "u_bob", 2000, expires_at=seconds_from_now(7200))
            ]
        )
    )
    tok = relogin(api, "ada", "bob")
    body = me(api, tok["ada"])
    assert body["held"] == 2000 and body["available"] == 8000
    assert find_auth(api, tok["ada"], "a_future")["status"] == "open"


def test_capture_of_expired_authorization_is_409_authorization_expired(api, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_past", "u_ada", "u_bob", 2000, expires_at=seconds_from_now(-7200))
            ]
        )
    )
    tok = relogin(api, "ada", "bob")
    expect(capture(api, tok["bob"], "a_past"), 409, "authorization_expired")


def test_void_of_expired_authorization_is_409_authorization_not_open(api, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_past", "u_ada", "u_bob", 2000, expires_at=seconds_from_now(-7200))
            ]
        )
    )
    tok = relogin(api, "ada", "bob")
    expect(void_authorization(api, tok["ada"], "a_past"), 409, "authorization_not_open")


def test_expired_matches_expired_status_filter_not_open(api, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_past", "u_ada", "u_bob", 2000, expires_at=seconds_from_now(-7200)),
                seeded_authorization("a_open", "u_ada", "u_bob", 1000, expires_at=seconds_from_now(7200)),
            ]
        )
    )
    tok = relogin(api, "ada")["ada"]
    expired = payload(api.get("/authorizations", token=tok, params={"status": "expired"}))["authorizations"]
    open_ = payload(api.get("/authorizations", token=tok, params={"status": "open"}))["authorizations"]
    assert [a["authorization_id"] for a in expired] == ["a_past"]
    assert [a["authorization_id"] for a in open_] == ["a_open"]


def test_created_authorization_uses_fixture_ttl_default(api, tokens):
    body = payload(authorize(api, tokens["ada"], "bob", 100))
    assert ttl_seconds(body) == 600


def test_created_authorization_uses_custom_fixture_ttl(api, reset):
    reset(make_fixture(authorization_ttl_seconds=120))
    tok = relogin(api, "ada")["ada"]
    body = payload(authorize(api, tok, "bob", 100))
    assert ttl_seconds(body) == 120
