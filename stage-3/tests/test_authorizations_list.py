"""Stage-2 §GET /authorizations: scope, filters and pagination."""

import time

import pytest

from support import (
    Api,
    authorize,
    expect,
    login,
    make_fixture,
    payload,
    seeded_authorization,
    seconds_from_now,
)


def auth_list(api, token, **params):
    resp = api.get("/authorizations", token=token, params=params or None)
    expect(resp, 200)
    return payload(resp)


def test_list_response_shape(api, tokens):
    body = auth_list(api, tokens["ada"])
    assert set(body) == {"authorizations", "has_more"}
    assert isinstance(body["authorizations"], list)


def test_list_returns_only_authorizations_involving_caller(api, tokens):
    ada_to_bob = payload(authorize(api, tokens["ada"], "bob", 100))["authorization_id"]
    bob_to_ada = payload(authorize(api, tokens["bob"], "ada", 100))["authorization_id"]
    ada_to_cy = payload(authorize(api, tokens["ada"], "cy", 100))["authorization_id"]

    ids = [a["authorization_id"] for a in auth_list(api, tokens["ada"])["authorizations"]]
    assert ada_to_bob in ids and bob_to_ada in ids and ada_to_cy in ids
    cy_ids = [a["authorization_id"] for a in auth_list(api, tokens["cy"])["authorizations"]]
    assert cy_ids == [ada_to_cy]


def test_direction_filter(api, tokens):
    outgoing = payload(authorize(api, tokens["ada"], "bob", 100))["authorization_id"]
    incoming = payload(authorize(api, tokens["bob"], "ada", 100))["authorization_id"]

    out = [a["authorization_id"] for a in auth_list(api, tokens["ada"], direction="outgoing")["authorizations"]]
    inc = [a["authorization_id"] for a in auth_list(api, tokens["ada"], direction="incoming")["authorizations"]]
    assert outgoing in out and outgoing not in inc
    assert incoming in inc and incoming not in out


def test_status_filter(api, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_open", "u_ada", "u_bob", 100, expires_at=seconds_from_now(7200)),
                seeded_authorization("a_void", "u_ada", "u_bob", 100, status="voided"),
            ]
        )
    )
    tok = payload(login(Api(), "ada@example.com"))["token"]
    open_ids = [a["authorization_id"] for a in auth_list(api, tok, status="open")["authorizations"]]
    void_ids = [a["authorization_id"] for a in auth_list(api, tok, status="voided")["authorizations"]]
    assert open_ids == ["a_open"]
    assert void_ids == ["a_void"]


@pytest.mark.parametrize("value", ["sideways", "OUTGOING", ""])
def test_unknown_direction_is_422(api, tokens, value):
    expect(api.get("/authorizations", token=tokens["ada"], params={"direction": value}), 422, "validation_failed")


@pytest.mark.parametrize("value", ["bogus", "OPEN", ""])
def test_unknown_status_is_422(api, tokens, value):
    expect(api.get("/authorizations", token=tokens["ada"], params={"status": value}), 422, "validation_failed")


def test_pagination_and_has_more(api, tokens):
    for _ in range(3):
        authorize(api, tokens["ada"], "bob", 1)
    page1 = auth_list(api, tokens["ada"], limit=2, offset=0)
    assert len(page1["authorizations"]) == 2 and page1["has_more"] is True
    page2 = auth_list(api, tokens["ada"], limit=2, offset=2)
    assert len(page2["authorizations"]) == 1 and page2["has_more"] is False


def test_list_is_newest_first_across_seconds(api, tokens):
    first = payload(authorize(api, tokens["ada"], "bob", 1))["authorization_id"]
    time.sleep(1.1)
    second = payload(authorize(api, tokens["ada"], "bob", 1))["authorization_id"]
    ids = [a["authorization_id"] for a in auth_list(api, tokens["ada"])["authorizations"]]
    assert ids.index(second) < ids.index(first)
