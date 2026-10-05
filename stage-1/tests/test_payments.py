"""§8 POST /payments: amount/note/visibility rules and atomic movement."""

import pytest

from support import assert_rfc3339, expect, payload, unique

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
# §11 mentions settlement_id on payments; §8's body example omits it, so accept
# it when present but require it to be null for an ordinary (non-settlement) payment.
OPTIONAL_PAYMENT_FIELDS = {"settlement_id"}


def pay(api, token, **body):
    body.setdefault("to_handle", "bob")
    body.setdefault("amount", 100)
    return api.post("/payments", token=token, idem=unique("pay"), body=body)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_payment_returns_documented_body_and_moves_money(api, tokens):
    resp = pay(api, tokens["ada"], amount=1500, note="dinner", visibility="public")
    expect(resp, 201)
    body = payload(resp)
    assert REQUIRED_PAYMENT_FIELDS <= set(body)
    assert set(body) <= REQUIRED_PAYMENT_FIELDS | OPTIONAL_PAYMENT_FIELDS
    assert body.get("settlement_id") is None
    assert body["from_user_id"] == "u_ada" and body["from_handle"] == "ada"
    assert body["to_user_id"] == "u_bob" and body["to_handle"] == "bob"
    assert body["amount"] == 1500 and body["currency"] == "EUR"
    assert body["note"] == "dinner" and body["visibility"] == "public"
    assert body["request_id"] is None
    assert_rfc3339(body["created_at"], "created_at")

    assert payload(api.get("/me", token=tokens["ada"]))["balance"] == 8500
    assert payload(api.get("/me", token=tokens["bob"]))["balance"] == 4000


def test_payment_defaults_note_empty_and_visibility_public(api, tokens):
    body = payload(pay(api, tokens["ada"], amount=10))
    assert body["note"] == ""
    assert body["visibility"] == "public"


def test_payment_of_exact_balance_is_allowed(api, tokens):
    expect(pay(api, tokens["bob"], to_handle="ada", amount=2500), 201)
    assert payload(api.get("/me", token=tokens["bob"]))["balance"] == 0


def test_payment_note_round_trips_unicode_and_emoji(api, tokens):
    note = "café ☕ \u00e9\u0301 🎉 \"quotes\" \\slash"
    body = payload(pay(api, tokens["ada"], amount=5, note=note))
    assert body["note"] == note


def test_payment_is_visible_symmetrically_in_activity(api, tokens):
    resp = pay(api, tokens["ada"], amount=100, visibility="private")
    pid = payload(resp)["payment_id"]
    for handle in ("ada", "bob"):
        ids = [p["payment_id"] for p in payload(api.get("/activity", token=tokens[handle]))["payments"]]
        assert pid in ids


# --------------------------------------------------------------------------- #
# Negative cases
# --------------------------------------------------------------------------- #


def test_payment_to_self_is_422_self_payment(api, tokens):
    expect(pay(api, tokens["ada"], to_handle="ada", amount=10), 422, "self_payment")


def test_payment_to_unknown_handle_is_404(api, tokens):
    expect(pay(api, tokens["ada"], to_handle="ghost", amount=10), 404, "not_found")


def test_payment_insufficient_funds_is_409_and_leaves_no_trace(api, tokens):
    resp = pay(api, tokens["bob"], to_handle="ada", amount=2501)
    expect(resp, 409, "insufficient_funds")
    assert payload(api.get("/me", token=tokens["bob"]))["balance"] == 2500
    assert payload(api.get("/me", token=tokens["ada"]))["balance"] == 10000
    assert payload(api.get("/activity", token=tokens["ada"]))["payments"] == []


@pytest.mark.parametrize("amount", [0, -1, 1000000001])
def test_payment_amount_out_of_range_is_422(api, tokens, amount):
    expect(pay(api, tokens["ada"], amount=amount), 422, "validation_failed")


def test_payment_amount_at_max_is_accepted_when_funded(api, tokens):
    # 1_000_000_000 is the documented maximum; this caller cannot afford it,
    # so the validation boundary shows up as 409 (not 422).
    expect(pay(api, tokens["ada"], amount=1000000000), 409, "insufficient_funds")


def test_payment_note_of_200_chars_is_accepted(api, tokens):
    expect(pay(api, tokens["ada"], amount=1, note="n" * 200), 201)


def test_payment_note_over_200_chars_is_422(api, tokens):
    expect(pay(api, tokens["ada"], amount=1, note="n" * 201), 422, "validation_failed")


@pytest.mark.parametrize("note", [None, 12, True, [], {"a": 1}])
def test_payment_note_non_string_is_422(api, tokens, note):
    expect(pay(api, tokens["ada"], amount=1, note=note), 422, "validation_failed")


@pytest.mark.parametrize("visibility", ["Public", "private ", "", "hidden", 1, None])
def test_payment_invalid_visibility_is_422(api, tokens, visibility):
    expect(pay(api, tokens["ada"], amount=1, visibility=visibility), 422, "validation_failed")


@pytest.mark.parametrize("amount", ["100", True, False, None, [], {}])
def test_payment_amount_non_integer_is_422(api, tokens, amount):
    expect(pay(api, tokens["ada"], amount=amount), 422, "validation_failed")


def test_payment_to_handle_wrong_json_type_is_400(api, tokens):
    expect(pay(api, tokens["ada"], to_handle=123, amount=1), 400, "malformed_request")


@pytest.mark.parametrize("missing", ["to_handle", "amount"])
def test_payment_missing_required_field_is_422(api, tokens, missing):
    body = {"to_handle": "bob", "amount": 1}
    del body[missing]
    expect(api.post("/payments", token=tokens["ada"], idem=unique("pay"), body=body), 422, "validation_failed")


def test_payment_unparseable_body_is_400(api, tokens):
    resp = api.post("/payments", token=tokens["ada"], idem=unique("pay"), raw_body="{not json")
    expect(resp, 400, "malformed_request")


def test_payment_unknown_fields_are_ignored(api, tokens):
    resp = api.post(
        "/payments",
        token=tokens["ada"],
        idem=unique("pay"),
        body={"to_handle": "bob", "amount": 1, "surprise": {"x": 1}, "visibility": "public"},
    )
    expect(resp, 201)
