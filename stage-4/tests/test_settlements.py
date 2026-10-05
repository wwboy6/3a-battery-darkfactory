"""§11 atomic net settlements."""

import pytest

from support import (
    assert_rfc3339,
    expect,
    login,
    make_fixture,
    payload,
    unique,
    user,
)


def transfer(frm, to, amount, **extra):
    body = {"from_handle": frm, "to_handle": to, "amount": amount}
    body.update(extra)
    return body


def settle(api, token, transfers, key=None):
    return api.post("/settlements", token=token, idem=key or unique("stl"), body={"transfers": transfers})


@pytest.fixture
def op(api, reset):
    reset(make_fixture(settlement_operator_ids=["u_ada"]))
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in ("ada", "bob", "cy")}


def custom_env(api, reset, users, operators=("u_ada",)):
    reset(make_fixture(users=users, settlement_operator_ids=list(operators)))
    return {u["handle"]: payload(login(api, u["email"]))["token"] for u in users}


# --------------------------------------------------------------------------- #
# Authorisation
# --------------------------------------------------------------------------- #


def test_settlement_without_token_is_401(api, op):
    expect(
        api.post("/settlements", idem=unique("stl"), body={"transfers": [transfer("ada", "bob", 1)]}),
        401,
        "unauthenticated",
    )


def test_settlement_by_non_operator_is_403(api, op):
    expect(settle(api, op["bob"], [transfer("ada", "bob", 1)]), 403, "forbidden")


def test_operator_is_determined_by_fixture(api, reset):
    tok = custom_env(api, reset, [user("u_ada", "ada", 100), user("u_bob", "bob", 0)], operators=["u_bob"])
    expect(settle(api, tok["ada"], [transfer("ada", "bob", 1)]), 403, "forbidden")
    expect(settle(api, tok["bob"], [transfer("ada", "bob", 1)]), 201)


# --------------------------------------------------------------------------- #
# Happy path / response shape
# --------------------------------------------------------------------------- #


def test_single_transfer_settlement(api, op):
    resp = settle(api, op["ada"], [transfer("ada", "bob", 100)])
    expect(resp, 201)
    body = payload(resp)
    assert set(body) == {"settlement_id", "committed_at", "payments"}
    assert isinstance(body["settlement_id"], str) and 0 < len(body["settlement_id"]) <= 64
    assert_rfc3339(body["committed_at"], "committed_at")
    assert len(body["payments"]) == 1

    payment = body["payments"][0]
    assert payment["settlement_id"] == body["settlement_id"]
    assert payment["request_id"] is None
    assert payment["from_handle"] == "ada" and payment["to_handle"] == "bob"
    assert payment["amount"] == 100
    assert payment["created_at"] == body["committed_at"]
    assert payload(api.get("/me", token=op["ada"]))["balance"] == 9900
    assert payload(api.get("/me", token=op["bob"]))["balance"] == 2600


def test_settlement_payments_are_in_input_order(api, op):
    resp = settle(
        api,
        op["ada"],
        [transfer("ada", "bob", 30), transfer("bob", "cy", 20), transfer("cy", "ada", 10)],
    )
    expect(resp, 201)
    body = payload(resp)
    assert [(p["from_handle"], p["to_handle"], p["amount"]) for p in body["payments"]] == [
        ("ada", "bob", 30),
        ("bob", "cy", 20),
        ("cy", "ada", 10),
    ]
    committed = body["committed_at"]
    assert all(p["created_at"] == committed for p in body["payments"])
    assert all(p["settlement_id"] == body["settlement_id"] for p in body["payments"])


def test_ordinary_payment_has_null_settlement_id(api, op):
    resp = api.post(
        "/payments",
        token=op["ada"],
        idem=unique("pay"),
        body={"to_handle": "bob", "amount": 5},
    )
    expect(resp, 201)
    assert payload(resp).get("settlement_id") is None


def test_settlement_replay_returns_original_response(api, op):
    key = unique("stl")
    transfers = [transfer("ada", "bob", 100)]
    first = settle(api, op["ada"], transfers, key=key)
    expect(first, 201)
    replay = settle(api, op["ada"], transfers, key=key)
    expect(replay, 200)
    assert payload(replay) == payload(first)
    assert payload(api.get("/me", token=op["ada"]))["balance"] == 9900


def test_settlement_defaults_and_visibility(api, op):
    body = payload(settle(api, op["ada"], [transfer("ada", "bob", 1), transfer("ada", "bob", 1, note="x", visibility="private")]))
    assert body["payments"][0]["note"] == "" and body["payments"][0]["visibility"] == "public"
    assert body["payments"][1]["note"] == "x" and body["payments"][1]["visibility"] == "private"


def test_settlement_ignores_unknown_fields(api, op):
    expect(settle(api, op["ada"], [transfer("ada", "bob", 1, surprise="ok")]), 201)


# --------------------------------------------------------------------------- #
# Collective affordability / atomicity
# --------------------------------------------------------------------------- #


def test_collectively_affordable_chain_succeeds(api, reset):
    """No single wallet covers its outflow, but the net batch balances."""
    tok = custom_env(api, reset, [user("u_ada", "ada", 100), user("u_bob", "bob", 0), user("u_cy", "cy", 0)])
    resp = settle(api, tok["ada"], [transfer("ada", "bob", 100), transfer("bob", "cy", 100)])
    expect(resp, 201)
    assert payload(api.get("/me", token=tok["ada"]))["balance"] == 0
    assert payload(api.get("/me", token=tok["bob"]))["balance"] == 0
    assert payload(api.get("/me", token=tok["cy"]))["balance"] == 100


def test_unaffordable_settlement_is_409_and_changes_nothing(api, reset):
    tok = custom_env(api, reset, [user("u_ada", "ada", 50), user("u_bob", "bob", 0)])
    before = {h: payload(api.get("/me", token=tok[h]))["balance"] for h in ("ada", "bob")}
    expect(settle(api, tok["ada"], [transfer("ada", "bob", 100)]), 409, "insufficient_funds")
    after = {h: payload(api.get("/me", token=tok[h]))["balance"] for h in ("ada", "bob")}
    assert after == before
    assert payload(api.get("/activity", token=tok["ada"]))["payments"] == []


def test_failed_settlement_does_not_claim_the_key(api, reset):
    tok = custom_env(api, reset, [user("u_ada", "ada", 50), user("u_bob", "bob", 0)])
    key = unique("stl")
    expect(settle(api, tok["ada"], [transfer("ada", "bob", 100)], key=key), 409, "insufficient_funds")
    expect(settle(api, tok["ada"], [transfer("ada", "bob", 10)], key=key), 201)


# --------------------------------------------------------------------------- #
# Entry validation
# --------------------------------------------------------------------------- #


def test_settlement_unknown_handle_is_404(api, op):
    expect(settle(api, op["ada"], [transfer("ghost", "bob", 1)]), 404, "not_found")


def test_settlement_self_transfer_is_422_self_payment(api, op):
    expect(settle(api, op["ada"], [transfer("ada", "ada", 1)]), 422, "self_payment")


def test_entry_errors_take_precedence_in_input_order(api, op):
    # Unknown handle (404) comes first, so it wins over the later self-transfer.
    expect(settle(api, op["ada"], [transfer("ghost", "bob", 1), transfer("ada", "ada", 1)]), 404)
    # Self-transfer first wins over the later unknown handle.
    expect(settle(api, op["ada"], [transfer("ada", "ada", 1), transfer("ghost", "bob", 1)]), 422, "self_payment")


def test_entry_errors_before_insufficient_funds(api, reset):
    tok = custom_env(api, reset, [user("u_ada", "ada", 0), user("u_bob", "bob", 0)])
    # A valid but unaffordable entry precedes the self-transfer; entry errors win.
    expect(
        settle(api, tok["ada"], [transfer("ada", "bob", 100), transfer("ada", "ada", 1)]),
        422,
        "self_payment",
    )


@pytest.mark.parametrize("amount", [0, -1, 1000000001, "100", True])
def test_settlement_bad_transfer_amount_is_422(api, op, amount):
    expect(settle(api, op["ada"], [transfer("ada", "bob", amount)]), 422, "validation_failed")


def test_settlement_note_over_200_is_422(api, op):
    expect(settle(api, op["ada"], [transfer("ada", "bob", 1, note="x" * 201)]), 422, "validation_failed")


def test_settlement_bad_visibility_is_422(api, op):
    expect(settle(api, op["ada"], [transfer("ada", "bob", 1, visibility="secret")]), 422, "validation_failed")


# --------------------------------------------------------------------------- #
# Batch shape / size
# --------------------------------------------------------------------------- #


def test_settlement_empty_batch_is_422(api, op):
    expect(settle(api, op["ada"], []), 422, "validation_failed")


def test_settlement_missing_transfers_is_422(api, op):
    resp = api.post("/settlements", token=op["ada"], idem=unique("stl"), body={})
    expect(resp, 422, "validation_failed")


def test_settlement_transfers_wrong_type_is_400(api, op):
    resp = api.post("/settlements", token=op["ada"], idem=unique("stl"), body={"transfers": "nope"})
    expect(resp, 400, "malformed_request")


def test_settlement_max_32_transfers_is_accepted(api, reset):
    tok = custom_env(api, reset, [user("u_ada", "ada", 1000), user("u_bob", "bob", 1000)])
    transfers = []
    for i in range(32):
        transfers.append(transfer("ada", "bob", 1) if i % 2 == 0 else transfer("bob", "ada", 1))
    expect(settle(api, tok["ada"], transfers), 201)


def test_settlement_33_transfers_is_422(api, reset):
    tok = custom_env(api, reset, [user("u_ada", "ada", 1000), user("u_bob", "bob", 1000)])
    transfers = [transfer("ada", "bob", 1) for _ in range(33)]
    expect(settle(api, tok["ada"], transfers), 422, "validation_failed")


# --------------------------------------------------------------------------- #
# Permission scope
# --------------------------------------------------------------------------- #


def test_operator_cannot_see_third_party_private_activity(api, op):
    api.post(
        "/payments",
        token=op["bob"],
        idem=unique("pay"),
        body={"to_handle": "cy", "amount": 5, "visibility": "private"},
    )
    assert payload(api.get("/activity", token=op["ada"]))["payments"] == []
