"""§7 Idempotency: replay, reuse, scope and precedence."""

import pytest

from support import expect, login, make_fixture, payload, unique


def pay_body(to_handle="bob", amount=10):
    return {"to_handle": to_handle, "amount": amount}


def make_request(api, token, payer="ada", amount=100):
    return payload(
        api.post("/requests", token=token, idem=unique("req"), body={"payer_handle": payer, "amount": amount})
    )["request_id"]


def operator_tokens(api, reset):
    reset(make_fixture(settlement_operator_ids=["u_ada"]))
    out = {}
    for handle in ("ada", "bob", "cy"):
        out[handle] = payload(login(api, f"{handle}@example.com"))["token"]
    return out


# --------------------------------------------------------------------------- #
# Missing / invalid keys
# --------------------------------------------------------------------------- #


def test_each_write_path_requires_an_idempotency_key(api, reset):
    op = operator_tokens(api, reset)
    rid = make_request(api, op["bob"])
    settle = {"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 1}]}
    cases = [
        ("/payments", op["ada"], pay_body()),
        ("/requests", op["bob"], {"payer_handle": "ada", "amount": 5}),
        (f"/requests/{rid}/pay", op["ada"], {}),
        ("/splits", op["ada"], {"amount": 10, "participant_handles": ["ada", "bob"]}),
        ("/settlements", op["ada"], settle),
    ]
    for path, token, body in cases:
        expect(api.post(path, token=token, body=body), 400, "missing_idempotency_key")


def test_empty_idempotency_key_is_400(api, tokens):
    expect(
        api.post("/payments", token=tokens["ada"], idem="", body=pay_body()),
        400,
        "missing_idempotency_key",
    )


def test_idempotency_key_over_255_chars_is_422(api, tokens):
    expect(
        api.post("/payments", token=tokens["ada"], idem="k" * 256, body=pay_body()),
        422,
        "validation_failed",
    )


def test_idempotency_key_of_255_chars_is_accepted(api, tokens):
    expect(api.post("/payments", token=tokens["ada"], idem="k" * 255, body=pay_body()), 201)


# --------------------------------------------------------------------------- #
# Replay semantics
# --------------------------------------------------------------------------- #


def test_first_use_is_201_and_replay_is_200_with_identical_body(api, tokens):
    key = unique("idem")
    first = api.post("/payments", token=tokens["ada"], idem=key, body=pay_body(amount=100))
    expect(first, 201)
    second = api.post("/payments", token=tokens["ada"], idem=key, body=pay_body(amount=100))
    expect(second, 200)
    assert payload(second) == payload(first)
    # Only one money movement happened.
    assert payload(api.get("/me", token=tokens["ada"]))["balance"] == 9900


def test_same_key_different_body_is_409(api, tokens):
    key = unique("idem")
    expect(api.post("/payments", token=tokens["ada"], idem=key, body=pay_body(amount=100)), 201)
    expect(api.post("/payments", token=tokens["ada"], idem=key, body=pay_body(amount=200)), 409, "idempotency_key_reuse")


def test_replay_identity_ignores_json_key_order(api, tokens):
    key = unique("idem")
    first = api.post("/payments", token=tokens["ada"], idem=key, raw_body='{"to_handle": "bob", "amount": 10}')
    expect(first, 201)
    second = api.post("/payments", token=tokens["ada"], idem=key, raw_body='{"amount":10,"to_handle":"bob"}')
    expect(second, 200)
    assert payload(second) == payload(first)


def test_pay_empty_body_and_explicit_public_are_different_bodies(api, tokens):
    rid = make_request(api, tokens["bob"])
    key = unique("idem")
    expect(api.post(f"/requests/{rid}/pay", token=tokens["ada"], idem=key, body={}), 201)
    expect(
        api.post(f"/requests/{rid}/pay", token=tokens["ada"], idem=key, body={"visibility": "public"}),
        409,
        "idempotency_key_reuse",
    )


def test_replay_returns_original_response_after_state_changed(api, tokens):
    key = unique("idem")
    first = api.post("/payments", token=tokens["ada"], idem=key, body=pay_body(amount=100))
    expect(first, 201)
    api.post("/payments", token=tokens["ada"], idem=unique("idem"), body=pay_body(amount=500))
    replay = api.post("/payments", token=tokens["ada"], idem=key, body=pay_body(amount=100))
    expect(replay, 200)
    assert payload(replay) == payload(first)
    # 10000 - 100 - 500 = 9400 (the replay moved nothing).
    assert payload(api.get("/me", token=tokens["ada"]))["balance"] == 9400


def test_claimed_key_resolved_before_field_validation(api, tokens):
    key = unique("idem")
    expect(api.post("/payments", token=tokens["ada"], idem=key, body=pay_body(amount=100)), 201)
    # Same key, now with an invalid body: reuse wins over validation.
    expect(api.post("/payments", token=tokens["ada"], idem=key, body=pay_body(amount=-5)), 409, "idempotency_key_reuse")


def test_key_is_scoped_per_user(api, tokens):
    key = unique("idem")
    expect(api.post("/payments", token=tokens["ada"], idem=key, body=pay_body(amount=10)), 201)
    expect(api.post("/payments", token=tokens["bob"], idem=key, body=pay_body("cy", 10)), 201)


def test_same_key_different_path_is_not_a_replay(api, tokens):
    key = unique("idem")
    expect(api.post("/payments", token=tokens["ada"], idem=key, body=pay_body(amount=10)), 201)
    expect(api.post("/requests", token=tokens["ada"], idem=key, body={"payer_handle": "bob", "amount": 10}), 201)


def test_key_reusable_after_4xx_failure(api, tokens):
    key = unique("idem")
    expect(api.post("/payments", token=tokens["ada"], idem=key, body=pay_body(amount=0)), 422, "validation_failed")
    expect(api.post("/payments", token=tokens["ada"], idem=key, body=pay_body(amount=10)), 201)


def test_key_reusable_after_insufficient_funds(api, tokens):
    key = unique("idem")
    expect(api.post("/payments", token=tokens["bob"], idem=key, body=pay_body("ada", 2501)), 409, "insufficient_funds")
    expect(api.post("/payments", token=tokens["bob"], idem=key, body=pay_body("ada", 10)), 201)


def test_replay_of_successful_request_pay_returns_payment_not_conflict(api, tokens):
    rid = make_request(api, tokens["bob"])
    key = unique("idem")
    first = api.post(f"/requests/{rid}/pay", token=tokens["ada"], idem=key, body={})
    expect(first, 201)
    replay = api.post(f"/requests/{rid}/pay", token=tokens["ada"], idem=key, body={})
    expect(replay, 200)
    assert payload(replay) == payload(first)
    assert payload(api.get("/me", token=tokens["ada"]))["balance"] == 9900


def test_replay_of_split_returns_original_and_creates_nothing_new(api, tokens):
    key = unique("idem")
    body = {"amount": 100, "participant_handles": ["ada", "bob"]}
    first = api.post("/splits", token=tokens["ada"], idem=key, body=body)
    expect(first, 201)
    replay = api.post("/splits", token=tokens["ada"], idem=key, body=body)
    expect(replay, 200)
    assert payload(replay) == payload(first)
    assert len(payload(api.get("/requests", token=tokens["bob"]))["requests"]) == 1


def test_replay_of_request_creation_is_200(api, tokens):
    key = unique("idem")
    body = {"payer_handle": "ada", "amount": 42}
    first = api.post("/requests", token=tokens["bob"], idem=key, body=body)
    expect(first, 201)
    replay = api.post("/requests", token=tokens["bob"], idem=key, body=body)
    expect(replay, 200)
    assert payload(replay) == payload(first)
