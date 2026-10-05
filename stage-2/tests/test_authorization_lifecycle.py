"""Stage-2 §authorizations: create, capture, void lifecycle."""

import pytest

from support import (
    AUTHORIZATION_FIELDS,
    assert_rfc3339,
    authorize,
    capture,
    expect,
    login,
    make_fixture,
    me,
    payload,
    seeded_authorization,
    ttl_seconds,
    void_authorization,
)


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def auth_list(api, token, **params):
    return payload(api.get("/authorizations", token=token, params=params or None))["authorizations"]


def find_auth(api, token, aid):
    return next(a for a in auth_list(api, token) if a["authorization_id"] == aid)


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #


def test_create_authorization_shape_and_defaults(api, tokens):
    resp = authorize(api, tokens["ada"], "bob", 2000)
    expect(resp, 201)
    body = payload(resp)
    assert AUTHORIZATION_FIELDS <= set(body)
    assert body["from_user_id"] == "u_ada" and body["from_handle"] == "ada"
    assert body["to_user_id"] == "u_bob" and body["to_handle"] == "bob"
    assert body["amount"] == 2000
    assert body["captured_amount"] == 0
    assert body["remaining_amount"] == 2000
    assert body["currency"] == "EUR"
    assert body["note"] == ""
    assert body["visibility"] == "public"
    assert body["status"] == "open"
    assert body["payment_id"] is None
    assert body["payment_ids"] == []
    assert_rfc3339(body["created_at"], "created_at")
    assert_rfc3339(body["expires_at"], "expires_at")
    assert ttl_seconds(body) == 600  # default TTL


def test_create_authorization_keeps_note_and_visibility(api, tokens):
    body = payload(authorize(api, tokens["ada"], "bob", 100, note="deposit", visibility="private"))
    assert body["note"] == "deposit" and body["visibility"] == "private"


def test_create_hold_moves_no_money(api, tokens):
    before = me(api, tokens["ada"])
    authorize(api, tokens["ada"], "bob", 2000)
    after = me(api, tokens["ada"])
    assert after["total"] == before["total"]  # no money moved
    assert after["held"] == 2000
    assert after["available"] == before["available"] - 2000


def test_open_authorization_is_not_a_feed_item(api, tokens):
    authorize(api, tokens["ada"], "bob", 2000)
    for handle in ("ada", "bob", "cy"):
        assert payload(api.get("/activity", token=tokens[handle]))["payments"] == []


def test_create_self_authorization_is_422_self_payment(api, tokens):
    expect(authorize(api, tokens["ada"], "ada", 10), 422, "self_payment")


def test_create_unknown_handle_is_404(api, tokens):
    expect(authorize(api, tokens["ada"], "ghost", 10), 404, "not_found")


@pytest.mark.parametrize("amount", [0, -1, 1000000001, "100", True, None])
def test_create_invalid_amount_is_422(api, tokens, amount):
    expect(authorize(api, tokens["ada"], "bob", amount), 422, "validation_failed")


def test_create_note_over_200_is_422(api, tokens):
    expect(authorize(api, tokens["ada"], "bob", 1, note="x" * 201), 422, "validation_failed")


@pytest.mark.parametrize("visibility", ["secret", "Public", "", 1, None])
def test_create_invalid_visibility_is_422(api, tokens, visibility):
    expect(authorize(api, tokens["ada"], "bob", 1, visibility=visibility), 422, "validation_failed")


def test_create_to_handle_wrong_type_is_400(api, tokens):
    expect(authorize(api, tokens["ada"], 123, 1), 400, "malformed_request")


def test_create_insufficient_available_is_409(api, tokens):
    authorize(api, tokens["ada"], "bob", 9000)  # available 1000 left
    expect(authorize(api, tokens["ada"], "bob", 2000), 409, "insufficient_funds")
    assert me(api, tokens["ada"])["held"] == 9000


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #


def test_capture_full_by_receiver_moves_money_and_closes(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    resp = capture(api, tokens["bob"], aid)
    expect(resp, 201)
    payment = payload(resp)
    assert payment["authorization_id"] == aid
    assert payment["request_id"] is None
    assert payment["amount"] == 2000
    assert payment["from_handle"] == "ada" and payment["to_handle"] == "bob"
    assert payment["visibility"] == "public"

    record = find_auth(api, tokens["bob"], aid)
    assert record["status"] == "captured"
    assert record["captured_amount"] == 2000
    assert record["remaining_amount"] == 0
    assert record["payment_id"] == payment["payment_id"]
    assert record["payment_ids"] == [payment["payment_id"]]

    assert me(api, tokens["ada"])["total"] == 8000
    assert me(api, tokens["ada"])["held"] == 0
    assert me(api, tokens["bob"])["total"] == 4500


def test_capture_partial_nonfinal_keeps_remainder_held(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    resp = capture(api, tokens["bob"], aid, amount=700, final=False)
    expect(resp, 201)
    assert payload(resp)["amount"] == 700

    record = find_auth(api, tokens["bob"], aid)
    assert record["status"] == "open"
    assert record["captured_amount"] == 700
    assert record["remaining_amount"] == 1300
    assert me(api, tokens["ada"])["held"] == 1300
    assert me(api, tokens["ada"])["total"] == 9300


def test_cumulative_captures_close_the_authorization(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    p1 = payload(capture(api, tokens["bob"], aid, amount=1500, final=False))
    p2 = payload(capture(api, tokens["bob"], aid))  # remainder 500

    record = find_auth(api, tokens["bob"], aid)
    assert record["status"] == "captured"
    assert record["captured_amount"] == 2000
    assert record["remaining_amount"] == 0
    assert record["payment_ids"] == [p1["payment_id"], p2["payment_id"]]
    assert record["payment_id"] == p2["payment_id"]
    assert me(api, tokens["ada"])["total"] == 8000


def test_capture_omitted_amount_defaults_to_remaining(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    capture(api, tokens["bob"], aid, amount=1500, final=False)
    resp = capture(api, tokens["bob"], aid)  # omitted => remaining 500
    expect(resp, 201)
    assert payload(resp)["amount"] == 500
    assert find_auth(api, tokens["bob"], aid)["status"] == "captured"


def test_capturing_entire_remainder_with_final_false_still_closes(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    resp = capture(api, tokens["bob"], aid, amount=2000, final=False)
    expect(resp, 201)
    record = find_auth(api, tokens["bob"], aid)
    assert record["status"] == "captured"
    assert record["remaining_amount"] == 0


def test_capture_exceeds_remainder_is_422(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    capture(api, tokens["bob"], aid, amount=1500, final=False)
    expect(capture(api, tokens["bob"], aid, amount=600), 422, "capture_exceeds_authorization")
    assert me(api, tokens["ada"])["held"] == 500


@pytest.mark.parametrize("amount", [0, -1, 1.5, "100", True])
def test_capture_invalid_amount_is_422(api, tokens, amount):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    expect(capture(api, tokens["bob"], aid, amount=amount), 422, "validation_failed")


def test_capture_amount_wrong_but_exceeding_is_capture_exceeds(api, tokens):
    """A well-typed amount above the remainder is capture_exceeds, not validation_failed."""
    aid = payload(authorize(api, tokens["ada"], "bob", 100))["authorization_id"]
    expect(capture(api, tokens["bob"], aid, amount=101), 422, "capture_exceeds_authorization")


def test_capture_on_closed_authorization_is_409(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    capture(api, tokens["bob"], aid)
    expect(capture(api, tokens["bob"], aid, amount=1), 409, "authorization_not_open")


def test_capture_by_payer_is_403(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    expect(capture(api, tokens["ada"], aid), 403, "forbidden")


def test_capture_by_third_party_is_403(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    expect(capture(api, tokens["cy"], aid), 403, "forbidden")


def test_capture_unknown_authorization_is_404(api, tokens):
    expect(capture(api, tokens["bob"], "a_missing"), 404, "not_found")


def test_captured_payment_follows_visibility_in_feed(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000, visibility="private"))["authorization_id"]
    pid = payload(capture(api, tokens["bob"], aid))["payment_id"]
    for handle in ("ada", "bob"):
        ids = [p["payment_id"] for p in payload(api.get("/activity", token=tokens[handle]))["payments"]]
        assert pid in ids
    assert pid not in [
        p["payment_id"] for p in payload(api.get("/activity", token=tokens["cy"]))["payments"]
    ]


# --------------------------------------------------------------------------- #
# Void
# --------------------------------------------------------------------------- #


def test_void_by_payer_releases_hold_and_repeat_is_200(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    first = void_authorization(api, tokens["ada"], aid)
    expect(first, 200)
    assert payload(first)["status"] == "voided"
    assert me(api, tokens["ada"])["held"] == 0

    second = void_authorization(api, tokens["ada"], aid)
    expect(second, 200)
    assert payload(second)["status"] == "voided"


def test_void_by_receiver_is_403(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    expect(void_authorization(api, tokens["bob"], aid), 403, "forbidden")


def test_void_by_third_party_is_403(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    expect(void_authorization(api, tokens["cy"], aid), 403, "forbidden")


def test_void_captured_authorization_is_409(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    capture(api, tokens["bob"], aid)
    expect(void_authorization(api, tokens["ada"], aid), 409, "authorization_not_open")


def test_void_after_partial_capture_releases_only_remainder(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    p1 = payload(capture(api, tokens["bob"], aid, amount=1000, final=False))
    expect(void_authorization(api, tokens["ada"], aid), 200)

    record = find_auth(api, tokens["ada"], aid)
    assert record["status"] == "voided"
    assert record["captured_amount"] == 1000
    assert record["remaining_amount"] == 0
    assert record["payment_ids"] == [p1["payment_id"]]
    # only the captured 1000 moved; the 1000 remainder was released
    assert me(api, tokens["ada"])["total"] == 9000
    assert me(api, tokens["ada"])["held"] == 0
    assert me(api, tokens["ada"])["available"] == 9000


def test_void_unknown_authorization_is_404(api, tokens):
    expect(void_authorization(api, tokens["ada"], "a_missing"), 404, "not_found")
