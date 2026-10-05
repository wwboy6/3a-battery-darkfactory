"""§8/§9 POST /splits: share rounding table and request generation."""

import pytest

from support import expect, login, make_fixture, payload, unique, user

SPLIT_FIELDS = {"split_id", "amount", "currency", "note", "shares", "requests", "created_at"}


def split(api, token, **body):
    body.setdefault("amount", 3000)
    body.setdefault("participant_handles", ["ada", "bob", "cy"])
    return api.post("/splits", token=token, idem=unique("split"), body=body)


def shares_of(body):
    return {s["handle"]: s["amount"] for s in body["shares"]}


def five_user_fixture():
    return make_fixture(
        users=[user(f"u_{h}", h, 0) for h in ("ada", "bob", "cy", "dave", "eve")]
    )


# --------------------------------------------------------------------------- #
# Happy path + shape
# --------------------------------------------------------------------------- #


def test_split_returns_documented_body(api, tokens):
    resp = split(api, tokens["ada"], amount=3000, participant_handles=["ada", "bob", "cy"], note="dinner")
    expect(resp, 201)
    body = payload(resp)
    assert set(body) == SPLIT_FIELDS
    assert body["amount"] == 3000 and body["currency"] == "EUR" and body["note"] == "dinner"
    assert [s["handle"] for s in body["shares"]] == ["ada", "bob", "cy"]
    assert sum(s["amount"] for s in body["shares"]) == 3000
    # requests exclude the caller, in participant order
    assert [r["payer_handle"] for r in body["requests"]] == ["bob", "cy"]
    for req in body["requests"]:
        assert req["requester_handle"] == "ada"
        assert req["status"] == "pending"


def test_split_note_defaults_to_empty(api, tokens):
    assert payload(split(api, tokens["ada"]))["note"] == ""


def test_split_does_not_check_any_balance(api, tokens):
    # cy has balance 0 and may still originate a split.
    resp = split(api, tokens["cy"], amount=1000, participant_handles=["cy", "ada"])
    expect(resp, 201)


def test_caller_only_split_creates_no_requests(api, tokens):
    body = payload(split(api, tokens["ada"], amount=500, participant_handles=["ada"]))
    assert body["shares"] == [{"handle": "ada", "amount": 500}]
    assert body["requests"] == []


# --------------------------------------------------------------------------- #
# §9 rounding table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "amount,handles,expected",
    [
        (1000, ["ada", "bob", "cy"], [334, 333, 333]),
        (1, ["ada", "bob", "cy"], [1, 0, 0]),
        (10, ["ada", "bob", "cy"], [4, 3, 3]),
        (999, ["ada", "bob", "cy"], [333, 333, 333]),
    ],
)
def test_split_rounding_table(api, tokens, amount, handles, expected):
    body = payload(split(api, tokens["ada"], amount=amount, participant_handles=handles))
    assert [s["amount"] for s in body["shares"]] == expected
    assert sum(expected) == amount


def test_split_rounding_five_ways(api, reset):
    reset(five_user_fixture())
    tok = payload(login(api, "ada@example.com"))["token"]
    handles = ["ada", "bob", "cy", "dave", "eve"]
    body = payload(split(api, tok, amount=5, participant_handles=handles))
    assert [s["amount"] for s in body["shares"]] == [1, 1, 1, 1, 1]
    assert [r["payer_handle"] for r in body["requests"]] == ["bob", "cy", "dave", "eve"]


def test_split_order_determines_who_gets_the_extra_unit(api, tokens):
    first = shares_of(payload(split(api, tokens["ada"], amount=1000, participant_handles=["ada", "bob", "cy"])))
    second = shares_of(payload(split(api, tokens["ada"], amount=1000, participant_handles=["cy", "bob", "ada"])))
    assert first == {"ada": 334, "bob": 333, "cy": 333}
    assert second == {"cy": 334, "bob": 333, "ada": 333}


def test_zero_share_still_creates_a_request(api, tokens):
    # 1 split two ways -> [1, 0]; the zero-share participant still gets a request.
    body = payload(split(api, tokens["ada"], amount=1, participant_handles=["bob", "cy"]))
    assert [s["amount"] for s in body["shares"]] == [1, 0]
    assert [r["payer_handle"] for r in body["requests"]] == ["bob", "cy"]
    assert [r["amount"] for r in body["requests"]] == [1, 0]


def test_split_requests_sum_back_to_amount(api, tokens):
    body = payload(split(api, tokens["ada"], amount=1000, participant_handles=["ada", "bob", "cy"]))
    assert sum(r["amount"] for r in body["requests"]) == 666  # 333 + 333, excluding caller ada's 334


# --------------------------------------------------------------------------- #
# Negative cases
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("amount", [0, -1, 1000000001, "100", True, None])
def test_split_invalid_amount_is_422(api, tokens, amount):
    expect(split(api, tokens["ada"], amount=amount), 422, "validation_failed")


def test_split_empty_participants_is_422(api, tokens):
    expect(split(api, tokens["ada"], participant_handles=[]), 422, "validation_failed")


def test_split_missing_participants_is_422(api, tokens):
    expect(api.post("/splits", token=tokens["ada"], idem=unique("split"), body={"amount": 10}), 422, "validation_failed")


def test_split_duplicate_participants_is_422(api, tokens):
    expect(split(api, tokens["ada"], participant_handles=["bob", "bob"]), 422, "validation_failed")


def test_split_unknown_handle_is_404(api, tokens):
    expect(split(api, tokens["ada"], participant_handles=["bob", "ghost"]), 404, "not_found")


def test_split_note_over_200_is_422(api, tokens):
    expect(split(api, tokens["ada"], note="x" * 201), 422, "validation_failed")


def test_split_participants_wrong_type_is_400(api, tokens):
    expect(
        api.post("/splits", token=tokens["ada"], idem=unique("split"), body={"amount": 10, "participant_handles": "bob"}),
        400,
        "malformed_request",
    )


# --------------------------------------------------------------------------- #
# Invariant independence
# --------------------------------------------------------------------------- #


def test_paying_all_split_requests_preserves_total(api, tokens):
    total_before = sum(payload(api.get("/me", token=tokens[h]))["balance"] for h in ("ada", "bob", "cy"))
    body = payload(split(api, tokens["ada"], amount=1000, participant_handles=["ada", "bob"]))
    req = body["requests"][0]
    expect(
        api.post(f"/requests/{req['request_id']}/pay", token=tokens["bob"], idem=unique("pay"), body={}),
        201,
    )
    total_after = sum(payload(api.get("/me", token=tokens[h]))["balance"] for h in ("ada", "bob", "cy"))
    assert total_after == total_before
