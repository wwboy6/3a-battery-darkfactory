"""§3.3/§4 reset + fixture semantics."""

from support import (
    expect,
    login,
    make_fixture,
    payload,
    seeded_payment,
    seeded_request,
    signup,
    user,
)


def test_reset_returns_204_with_empty_body(api):
    resp = api.post("/_test/reset", body=make_fixture())
    expect(resp, 204)
    assert resp.content == b""


def test_repeated_resets_replace_state(api):
    api.post("/_test/reset", body=make_fixture(users=[user("u_x", "x", 111)]))
    assert payload(login(api, "x@example.com"))["token"]
    expect(login(api, "ada@example.com"), 401, "unauthenticated")

    api.post("/_test/reset", body=make_fixture())
    assert payload(login(api, "ada@example.com"))["token"]
    expect(login(api, "x@example.com"), 401, "unauthenticated")


def test_reset_with_negative_balance_is_422_and_changes_nothing(api, reset):
    resp = api.post("/_test/reset", body=make_fixture(users=[user("u_x", "x", -1)]))
    expect(resp, 422, "validation_failed")
    # Previous (canonical) state must be untouched.
    token = payload(login(api, "ada@example.com"))["token"]
    assert payload(api.get("/me", token=token))["balance"] == 10000


def test_reset_replaces_all_service_state(api, reset, tokens):
    # Make some state: new user + a payment (tokens fixture is valid pre-reset).
    signup(api, "temp@example.com")
    api.post(
        "/payments",
        token=tokens["ada"],
        idem="reset-state-1",
        body={"to_handle": "bob", "amount": 100},
    )
    reset(make_fixture())
    expect(login(api, "temp@example.com"), 401, "unauthenticated")
    # Reset also clears tokens, so re-authenticate against the fresh state.
    fresh = payload(login(api, "ada@example.com"))["token"]
    feed = payload(api.get("/activity", token=fresh))
    assert feed["payments"] == []
    assert payload(api.get("/me", token=fresh))["balance"] == 10000


def test_reset_seeds_pending_requests(api, reset):
    reset(
        make_fixture(
            request_list=[seeded_request("rq_1", "u_bob", "u_ada", 1200, "taxi")],
        )
    )
    token = payload(login(api, "ada@example.com"))["token"]
    body = payload(api.get("/requests", token=token))
    assert [r["request_id"] for r in body["requests"]] == ["rq_1"]
    assert body["requests"][0]["status"] == "pending"


def test_reset_seeds_payments_and_activity(api, reset):
    reset(make_fixture(payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, "coffee")]))
    token = payload(login(api, "ada@example.com"))["token"]
    body = payload(api.get("/activity", token=token))
    assert [p["payment_id"] for p in body["payments"]] == ["p_1"]
    assert body["payments"][0]["note"] == "coffee"


def test_reset_accepts_settlement_operator_ids(api, reset):
    reset(make_fixture(settlement_operator_ids=["u_ada"]))
    token = payload(login(api, "ada@example.com"))["token"]
    resp = api.post(
        "/settlements",
        token=token,
        idem="op-reset-1",
        body={"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 1}]},
    )
    expect(resp, 201)


def test_reset_defaults_settlement_operators_to_empty(api, tokens):
    resp = api.post(
        "/settlements",
        token=tokens["ada"],
        idem="op-default-1",
        body={"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 1}]},
    )
    expect(resp, 403, "forbidden")
