"""§8 requests lifecycle: create, pay, decline, cancel, list."""

import time

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

REQUEST_FIELDS = {
    "request_id",
    "requester_id",
    "requester_handle",
    "payer_id",
    "payer_handle",
    "amount",
    "currency",
    "note",
    "status",
    "payment_id",
    "created_at",
}


def create_request(api, token, **body):
    body.setdefault("payer_handle", "ada")
    body.setdefault("amount", 1200)
    return api.post("/requests", token=token, idem=unique("req"), body=body)


def request_id_of(api, token, payer="ada"):
    return payload(create_request(api, token, payer_handle=payer))["request_id"]


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #


def test_create_request_returns_documented_body(api, tokens):
    resp = create_request(api, tokens["bob"], payer_handle="ada", amount=1200, note="taxi")
    expect(resp, 201)
    body = payload(resp)
    assert set(body) == REQUEST_FIELDS
    assert body["requester_id"] == "u_bob" and body["requester_handle"] == "bob"
    assert body["payer_id"] == "u_ada" and body["payer_handle"] == "ada"
    assert body["amount"] == 1200 and body["currency"] == "EUR"
    assert body["note"] == "taxi"
    assert body["status"] == "pending"
    assert body["payment_id"] is None
    assert_rfc3339(body["created_at"], "created_at")


def test_create_request_does_not_check_payer_balance(api, tokens):
    """A request above the payer's balance is legal and stays pending."""
    resp = create_request(api, tokens["bob"], payer_handle="ada", amount=500000)
    expect(resp, 201)
    assert payload(resp)["status"] == "pending"


def test_create_request_note_defaults_to_empty(api, tokens):
    assert payload(create_request(api, tokens["bob"]))["note"] == ""


def test_create_request_to_self_is_422_self_request(api, tokens):
    expect(create_request(api, tokens["bob"], payer_handle="bob"), 422, "self_request")


def test_create_request_unknown_payer_is_404(api, tokens):
    expect(create_request(api, tokens["bob"], payer_handle="ghost"), 404, "not_found")


@pytest.mark.parametrize("amount", [0, -5, 1000000001, "100", True, None])
def test_create_request_invalid_amount_is_422(api, tokens, amount):
    expect(create_request(api, tokens["bob"], amount=amount), 422, "validation_failed")


def test_create_request_note_over_200_is_422(api, tokens):
    expect(create_request(api, tokens["bob"], note="x" * 201), 422, "validation_failed")


def test_create_request_payer_handle_wrong_type_is_400(api, tokens):
    expect(api.post("/requests", token=tokens["bob"], idem=unique("req"), body={"payer_handle": 9, "amount": 1}), 400, "malformed_request")


@pytest.mark.parametrize("missing", ["payer_handle", "amount"])
def test_create_request_missing_required_field_is_422(api, tokens, missing):
    body = {"payer_handle": "ada", "amount": 1}
    del body[missing]
    expect(api.post("/requests", token=tokens["bob"], idem=unique("req"), body=body), 422, "validation_failed")


# --------------------------------------------------------------------------- #
# Pay
# --------------------------------------------------------------------------- #


def test_payer_can_pay_request(api, tokens):
    rid = request_id_of(api, tokens["bob"], payer="ada")
    resp = api.post(f"/requests/{rid}/pay", token=tokens["ada"], idem=unique("pay"), body={})
    expect(resp, 201)
    payment = payload(resp)
    assert payment["request_id"] == rid
    assert payment["from_handle"] == "ada" and payment["to_handle"] == "bob"
    assert payment["amount"] == 1200
    assert payment["visibility"] == "public"

    listed = payload(api.get("/requests", token=tokens["bob"]))["requests"][0]
    assert listed["status"] == "paid"
    assert listed["payment_id"] == payment["payment_id"]
    assert payload(api.get("/me", token=tokens["ada"]))["balance"] == 8800
    assert payload(api.get("/me", token=tokens["bob"]))["balance"] == 3700


def test_pay_request_private_visibility(api, tokens):
    rid = request_id_of(api, tokens["bob"], payer="ada")
    resp = api.post(f"/requests/{rid}/pay", token=tokens["ada"], idem=unique("pay"), body={"visibility": "private"})
    expect(resp, 201)
    assert payload(resp)["visibility"] == "private"


def test_pay_request_invalid_visibility_is_422(api, tokens):
    rid = request_id_of(api, tokens["bob"], payer="ada")
    resp = api.post(f"/requests/{rid}/pay", token=tokens["ada"], idem=unique("pay"), body={"visibility": "nope"})
    expect(resp, 422, "validation_failed")


def test_pay_request_by_non_payer_is_403(api, tokens):
    rid = request_id_of(api, tokens["bob"], payer="ada")
    # cy is neither requester nor payer
    expect(api.post(f"/requests/{rid}/pay", token=tokens["cy"], idem=unique("pay"), body={}), 403, "forbidden")


def test_pay_unknown_request_is_404(api, tokens):
    expect(api.post("/requests/rq_missing/pay", token=tokens["ada"], idem=unique("pay"), body={}), 404, "not_found")


def test_pay_already_paid_request_is_409_request_not_pending(api, tokens):
    rid = request_id_of(api, tokens["bob"], payer="ada")
    expect(api.post(f"/requests/{rid}/pay", token=tokens["ada"], idem=unique("pay"), body={}), 201)
    expect(api.post(f"/requests/{rid}/pay", token=tokens["ada"], idem=unique("pay"), body={}), 409, "request_not_pending")


def test_pay_request_while_short_is_409_then_payable_after_topup(api, reset):
    reset(
        make_fixture(
            users=[user("u_ada", "ada", 0), user("u_bob", "bob", 0), user("u_cy", "cy", 1000)]
        )
    )
    tok = {h: payload(login(api, f"{h}@example.com"))["token"] for h in ("ada", "bob", "cy")}
    rid = request_id_of(api, tok["bob"], payer="ada")
    expect(api.post(f"/requests/{rid}/pay", token=tok["ada"], idem=unique("pay"), body={}), 409, "insufficient_funds")
    # Money arrives later, then the same request becomes payable.
    expect(api.post("/payments", token=tok["cy"], idem=unique("pay"), body={"to_handle": "ada", "amount": 500}), 201)
    expect(api.post(f"/requests/{rid}/pay", token=tok["ada"], idem=unique("pay"), body={}), 201)
    assert payload(api.get("/me", token=tok["ada"]))["balance"] == 0
    assert payload(api.get("/me", token=tok["bob"]))["balance"] == 500


# --------------------------------------------------------------------------- #
# Decline / cancel
# --------------------------------------------------------------------------- #


def test_payer_can_decline_and_decline_again_is_200(api, tokens):
    rid = request_id_of(api, tokens["bob"], payer="ada")
    first = api.post(f"/requests/{rid}/decline", token=tokens["ada"])
    expect(first, 200)
    assert payload(first)["status"] == "declined"
    second = api.post(f"/requests/{rid}/decline", token=tokens["ada"])
    expect(second, 200)
    assert payload(second)["status"] == "declined"


def test_decline_by_non_payer_is_403(api, tokens):
    rid = request_id_of(api, tokens["bob"], payer="ada")
    expect(api.post(f"/requests/{rid}/decline", token=tokens["cy"]), 403, "forbidden")


def test_decline_paid_request_is_409(api, tokens):
    rid = request_id_of(api, tokens["bob"], payer="ada")
    expect(api.post(f"/requests/{rid}/pay", token=tokens["ada"], idem=unique("pay"), body={}), 201)
    expect(api.post(f"/requests/{rid}/decline", token=tokens["ada"]), 409, "request_not_pending")


def test_decline_unknown_request_is_404(api, tokens):
    expect(api.post("/requests/rq_missing/decline", token=tokens["ada"]), 404, "not_found")


def test_requester_can_cancel_and_cancel_again_is_200(api, tokens):
    rid = request_id_of(api, tokens["bob"], payer="ada")
    first = api.post(f"/requests/{rid}/cancel", token=tokens["bob"])
    expect(first, 200)
    assert payload(first)["status"] == "cancelled"
    second = api.post(f"/requests/{rid}/cancel", token=tokens["bob"])
    expect(second, 200)
    assert payload(second)["status"] == "cancelled"


def test_cancel_by_non_requester_is_403(api, tokens):
    rid = request_id_of(api, tokens["bob"], payer="ada")
    # ada is the payer, not the requester
    expect(api.post(f"/requests/{rid}/cancel", token=tokens["ada"]), 403, "forbidden")


def test_cancel_declined_request_is_409(api, tokens):
    rid = request_id_of(api, tokens["bob"], payer="ada")
    expect(api.post(f"/requests/{rid}/decline", token=tokens["ada"]), 200)
    expect(api.post(f"/requests/{rid}/cancel", token=tokens["bob"]), 409, "request_not_pending")


def test_cancel_paid_request_is_409(api, tokens):
    rid = request_id_of(api, tokens["bob"], payer="ada")
    expect(api.post(f"/requests/{rid}/pay", token=tokens["ada"], idem=unique("pay"), body={}), 201)
    expect(api.post(f"/requests/{rid}/cancel", token=tokens["bob"]), 409, "request_not_pending")


def test_decline_cancelled_request_is_409(api, tokens):
    rid = request_id_of(api, tokens["bob"], payer="ada")
    expect(api.post(f"/requests/{rid}/cancel", token=tokens["bob"]), 200)
    expect(api.post(f"/requests/{rid}/decline", token=tokens["ada"]), 409, "request_not_pending")


# --------------------------------------------------------------------------- #
# GET /requests
# --------------------------------------------------------------------------- #


def test_requests_visible_only_to_the_two_parties(api, tokens):
    rid = request_id_of(api, tokens["bob"], payer="ada")
    assert rid in [r["request_id"] for r in payload(api.get("/requests", token=tokens["bob"]))["requests"]]
    assert rid in [r["request_id"] for r in payload(api.get("/requests", token=tokens["ada"]))["requests"]]
    assert rid not in [r["request_id"] for r in payload(api.get("/requests", token=tokens["cy"]))["requests"]]


def test_requests_direction_filter(api, tokens):
    incoming = request_id_of(api, tokens["bob"], payer="ada")  # ada pays
    outgoing = request_id_of(api, tokens["ada"], payer="bob")  # ada is requester

    inc = [r["request_id"] for r in payload(api.get("/requests", token=tokens["ada"], params={"direction": "incoming"}))["requests"]]
    out = [r["request_id"] for r in payload(api.get("/requests", token=tokens["ada"], params={"direction": "outgoing"}))["requests"]]
    both = [r["request_id"] for r in payload(api.get("/requests", token=tokens["ada"]))["requests"]]
    assert incoming in inc and incoming not in out
    assert outgoing in out and outgoing not in inc
    assert set(both) == {incoming, outgoing}


def test_requests_status_filter(api, tokens):
    pending = request_id_of(api, tokens["bob"], payer="ada")
    paid = request_id_of(api, tokens["bob"], payer="ada")
    expect(api.post(f"/requests/{paid}/pay", token=tokens["ada"], idem=unique("pay"), body={}), 201)

    pending_ids = [r["request_id"] for r in payload(api.get("/requests", token=tokens["bob"], params={"status": "pending"}))["requests"]]
    paid_ids = [r["request_id"] for r in payload(api.get("/requests", token=tokens["bob"], params={"status": "paid"}))["requests"]]
    assert pending in pending_ids and pending not in paid_ids
    assert paid in paid_ids and paid not in pending_ids


@pytest.mark.parametrize("value", ["sideways", "INCOMING", " "])
def test_requests_unknown_direction_is_422(api, tokens, value):
    expect(api.get("/requests", token=tokens["ada"], params={"direction": value}), 422, "validation_failed")


@pytest.mark.parametrize("value", ["unknown", "PENDING", ""])
def test_requests_unknown_status_is_422(api, tokens, value):
    expect(api.get("/requests", token=tokens["ada"], params={"status": value}), 422, "validation_failed")


def test_requests_pagination_and_has_more(api, tokens):
    for _ in range(3):
        request_id_of(api, tokens["bob"], payer="ada")
    page1 = payload(api.get("/requests", token=tokens["bob"], params={"limit": 2, "offset": 0}))
    assert len(page1["requests"]) == 2 and page1["has_more"] is True
    page2 = payload(api.get("/requests", token=tokens["bob"], params={"limit": 2, "offset": 2}))
    assert len(page2["requests"]) == 1 and page2["has_more"] is False


def test_requests_are_newest_first(api, tokens):
    first = request_id_of(api, tokens["bob"], payer="ada")
    time.sleep(1.1)
    second = request_id_of(api, tokens["bob"], payer="ada")
    ids = [r["request_id"] for r in payload(api.get("/requests", token=tokens["bob"]))["requests"]]
    assert ids.index(second) < ids.index(first)


def test_requests_empty_for_user_with_none(api, tokens):
    assert payload(api.get("/requests", token=tokens["cy"]))["requests"] == []
