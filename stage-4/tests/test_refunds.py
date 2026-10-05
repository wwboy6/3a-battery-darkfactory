"""Stage-4 POST /payments/{id}/refunds: lifecycle, money math and errors."""

import pytest

from support import (
    assert_rfc3339,
    authorize,
    capture,
    create_payment,
    expect,
    login,
    me,
    payload,
    refund,
    unique,
)

REQUIRED_PAYMENT_FIELDS = {
    "payment_id",
    "from_user_id",
    "from_handle",
    "to_user_id",
    "to_handle",
    "amount",
    "currency",
    "note",
    "visibility",
    "request_id",
    "created_at",
}


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def pay(api, tokens, amount=500, to_handle="bob", **kwargs):
    resp = create_payment(api, tokens["ada"], to_handle, amount, **kwargs)
    expect(resp, 201)
    return payload(resp)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_refund_is_opposite_direction_with_linkage(api, tokens):
    original = pay(api, tokens, 500)
    resp = refund(api, tokens["bob"], original["payment_id"], amount=200)
    expect(resp, 201)
    body = payload(resp)

    assert REQUIRED_PAYMENT_FIELDS <= set(body)
    assert body["refund_of"] == original["payment_id"]
    assert body["from_user_id"] == "u_bob" and body["from_handle"] == "bob"
    assert body["to_user_id"] == "u_ada" and body["to_handle"] == "ada"
    assert body["amount"] == 200
    assert body["request_id"] is None
    assert body["authorization_id"] is None
    assert_rfc3339(body["created_at"], "created_at")

    assert me(api, tokens["bob"])["balance"] == 2800
    assert me(api, tokens["ada"])["balance"] == 9700


def test_ordinary_payment_has_null_refund_of(api, tokens):
    original = pay(api, tokens, 100)
    assert original.get("refund_of") is None


def test_refund_copies_note_and_visibility(api, tokens):
    original = pay(api, tokens, 300, note="dinner", visibility="private")
    body = payload(refund(api, tokens["bob"], original["payment_id"], amount=100))
    assert body["note"] == "dinner"
    assert body["visibility"] == "private"

    visible = [p["payment_id"] for p in payload(api.get("/activity", token=tokens["cy"]))["payments"]]
    assert body["payment_id"] not in visible  # still private to the two parties


def test_cumulative_refunds_may_equal_the_corrected_amount(api, tokens):
    original = pay(api, tokens, 500)
    expect(refund(api, tokens["bob"], original["payment_id"], amount=200), 201)
    expect(refund(api, tokens["bob"], original["payment_id"], amount=300), 201)
    assert me(api, tokens["bob"])["balance"] == 2500
    assert me(api, tokens["ada"])["balance"] == 10000


def test_refund_of_request_payment(api, tokens):
    rid = payload(
        api.post("/requests", token=tokens["bob"], idem=unique("req"), body={"payer_handle": "ada", "amount": 300})
    )["request_id"]
    paid = payload(api.post(f"/requests/{rid}/pay", token=tokens["ada"], idem=unique("pay"), body={}))
    body = payload(refund(api, tokens["bob"], paid["payment_id"], amount=100))
    assert body["refund_of"] == paid["payment_id"]
    assert body["request_id"] is None

    listed = payload(api.get("/requests", token=tokens["bob"]))["requests"][0]
    assert listed["status"] == "paid"
    assert listed["payment_id"] == paid["payment_id"]


def test_refund_of_capture_payment(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 1000))["authorization_id"]
    captured = payload(capture(api, tokens["bob"], aid, amount=400, final=False))
    body = payload(refund(api, tokens["bob"], captured["payment_id"], amount=150))
    assert body["refund_of"] == captured["payment_id"]
    assert body["authorization_id"] is None

    view = me(api, tokens["ada"])
    assert view["held"] == 600  # refund never restores a released hold


# --------------------------------------------------------------------------- #
# Authorisation / lookup
# --------------------------------------------------------------------------- #


def test_sender_cannot_refund(api, tokens):
    original = pay(api, tokens, 500)
    expect(refund(api, tokens["ada"], original["payment_id"], amount=100), 403, "forbidden")


def test_third_party_cannot_refund(api, tokens):
    original = pay(api, tokens, 500)
    expect(refund(api, tokens["cy"], original["payment_id"], amount=100), 403, "forbidden")


def test_refund_without_token_is_401(api, tokens):
    original = pay(api, tokens, 500)
    expect(
        api.post(f"/payments/{original['payment_id']}/refunds", idem=unique("ref"), body={"amount": 100}),
        401,
        "unauthenticated",
    )


def test_refund_unknown_payment_is_404(api, tokens):
    expect(refund(api, tokens["bob"], "p_missing", amount=100), 404, "not_found")


# --------------------------------------------------------------------------- #
# Validation and limits
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [{}, {"amount": 0}, {"amount": -1}, {"amount": "100"}, {"amount": 1.5}, {"amount": True}],
)
def test_refund_invalid_amount_is_422(api, tokens, body):
    original = pay(api, tokens, 500)
    expect(refund(api, tokens["bob"], original["payment_id"], body=body), 422, "validation_failed")


def test_refund_exceeding_corrected_amount_is_422(api, tokens):
    original = pay(api, tokens, 500)
    expect(refund(api, tokens["bob"], original["payment_id"], amount=600), 422, "refund_exceeds_payment")


def test_refund_of_refund_is_422_invalid_target(api, tokens):
    original = pay(api, tokens, 500)
    first = payload(refund(api, tokens["bob"], original["payment_id"], amount=200))
    expect(refund(api, tokens["ada"], first["payment_id"], amount=50), 422, "invalid_refund_target")
    expect(refund(api, tokens["bob"], first["payment_id"], amount=50), 403, "forbidden")


def test_refund_insufficient_available_is_409(api, tokens):
    original = pay(api, tokens, 500)  # bob 3000
    expect(create_payment(api, tokens["bob"], "cy", 2600), 201)  # bob 400
    expect(refund(api, tokens["bob"], original["payment_id"], amount=500), 409, "insufficient_funds")


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_refund_replay_returns_original_body(api, tokens):
    original = pay(api, tokens, 500)
    key = unique("ref")
    first = payload(refund(api, tokens["bob"], original["payment_id"], amount=200, key=key))
    replay = refund(api, tokens["bob"], original["payment_id"], amount=200, key=key)
    expect(replay, 200)
    assert payload(replay) == first
    assert me(api, tokens["bob"])["balance"] == 2800


def test_refund_key_reuse_with_different_body_is_409(api, tokens):
    original = pay(api, tokens, 500)
    key = unique("ref")
    expect(refund(api, tokens["bob"], original["payment_id"], amount=200, key=key), 201)
    expect(
        refund(api, tokens["bob"], original["payment_id"], amount=100, key=key),
        409,
        "idempotency_key_reuse",
    )


def test_failed_refund_key_remains_reusable(api, tokens):
    original = pay(api, tokens, 500)
    key = unique("ref")
    expect(refund(api, tokens["bob"], original["payment_id"], amount=600, key=key), 422, "refund_exceeds_payment")
    expect(refund(api, tokens["bob"], original["payment_id"], amount=100, key=key), 201)
    assert me(api, tokens["bob"])["balance"] == 2900
