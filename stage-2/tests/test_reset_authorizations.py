"""Stage-2: fixture/reset rules for authorizations and TTL."""

import pytest

from support import (
    expect,
    login,
    make_fixture,
    me,
    payload,
    seeded_authorization,
    user,
)


def relogin(api, handle="ada"):
    return payload(login(api, f"{handle}@example.com"))["token"]


def test_authorizations_omitted_means_empty(api, reset):
    reset(make_fixture())
    tok = relogin(api)
    assert payload(api.get("/authorizations", token=tok))["authorizations"] == []
    assert me(api, tok)["held"] == 0


def test_seeded_open_hold_reduces_available(api, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    body = me(api, relogin(api))
    assert body["held"] == 2000 and body["available"] == 8000 and body["total"] == 10000


@pytest.mark.parametrize("ttl", [0, -1, 1.5])
def test_invalid_ttl_is_422_and_changes_nothing(api, reset, ttl):
    resp = api.post("/_test/reset", body=make_fixture(authorization_ttl_seconds=ttl))
    expect(resp, 422, "validation_failed")
    body = me(api, relogin(api))
    assert body["balance"] == 10000 and body["held"] == 0


def test_single_open_hold_exceeding_balance_is_422_and_changes_nothing(api, reset):
    resp = api.post(
        "/_test/reset",
        body=make_fixture(authorizations=[seeded_authorization("a_big", "u_ada", "u_bob", 10001)]),
    )
    expect(resp, 422, "validation_failed")
    body = me(api, relogin(api))
    assert body["balance"] == 10000 and body["held"] == 0


def test_multiple_open_holds_exceeding_balance_is_422(api, reset):
    resp = api.post(
        "/_test/reset",
        body=make_fixture(
            authorizations=[
                seeded_authorization("a_1", "u_ada", "u_bob", 6000),
                seeded_authorization("a_2", "u_ada", "u_cy", 5000),
            ]
        ),
    )
    expect(resp, 422, "validation_failed")


def test_closed_seeded_holds_do_not_count_against_balance(api, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_huge", "u_ada", "u_bob", 20000, status="captured"),
                seeded_authorization("a_void", "u_ada", "u_bob", 20000, status="voided"),
                seeded_authorization("a_exp", "u_ada", "u_bob", 20000, status="expired"),
            ]
        )
    )
    body = me(api, relogin(api))
    assert body["held"] == 0 and body["available"] == 10000


def test_negative_seeded_balance_still_422(api, reset):
    resp = api.post("/_test/reset", body=make_fixture(users=[user("u_x", "x", -1)]))
    expect(resp, 422, "validation_failed")
