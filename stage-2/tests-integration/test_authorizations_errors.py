"""Authorization and capture error paths, precedence and idempotency edges."""

from __future__ import annotations

import json

from support import (
    LONG_AGO,
    expect,
    make_fixture,
    me,
    payload,
    seeded_authorization,
    unique,
)


def create(api, token, body, key=None):
    return api.post("/authorizations", token=token, idem=key or unique("auth"), body=body)


def capture(api, token, aid, body, key=None):
    return api.post(
        "/authorizations/%s/capture" % aid,
        token=token,
        idem=key or unique("cap"),
        body=body,
    )


def json_equals(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# --------------------------------------------------------------------------- #
# Authorization creation errors
# --------------------------------------------------------------------------- #


def test_authorize_beyond_available_is_refused(api, ada_token, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_cy", 9000)]))
    refused = create(api, ada_token, {"to_handle": "bob", "amount": 2000})
    expect(refused, 409, "insufficient_funds")
    assert me(api, ada_token)["available"] == 1000  # nothing changed


def test_authorize_requires_idempotency_key(api, ada_token):
    resp = api.post("/authorizations", token=ada_token, body={"to_handle": "bob", "amount": 100})
    expect(resp, 400, "missing_idempotency_key")


def test_authorize_field_validation(api, ada_token):
    expect(create(api, ada_token, {"to_handle": "bob", "amount": 0}), 422, "validation_failed")
    expect(create(api, ada_token, {"to_handle": "bob", "amount": "100"}), 422, "validation_failed")
    expect(create(api, ada_token, {"to_handle": "ada", "amount": 100}), 422, "self_payment")
    expect(create(api, ada_token, {"to_handle": "nobody", "amount": 100}), 404, "not_found")
    expect(
        create(api, ada_token, {"to_handle": "bob", "amount": 100, "note": "x" * 201}),
        422,
        "validation_failed",
    )
    expect(
        create(api, ada_token, {"to_handle": "bob", "amount": 100, "visibility": "secret"}),
        422,
        "validation_failed",
    )


def test_authorize_replay_moves_hold_once(api, ada_token):
    key = unique("auth-replay")
    body = {"to_handle": "bob", "amount": 2000}
    first = create(api, ada_token, body, key=key)
    expect(first, 201)
    replay = create(api, ada_token, body, key=key)
    expect(replay, 200)
    assert json_equals(payload(replay), payload(first))
    assert me(api, ada_token)["held"] == 2000  # one hold, not two
    expect(create(api, ada_token, {"to_handle": "bob", "amount": 99}, key=key), 409, "idempotency_key_reuse")


# --------------------------------------------------------------------------- #
# Capture errors and precedence
# --------------------------------------------------------------------------- #


def test_capture_requires_idempotency_key(api, ada_token, bob_token, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    resp = api.post("/authorizations/a_1/capture", token=bob_token, body={})
    expect(resp, 400, "missing_idempotency_key")


def test_capture_amount_validation(api, ada_token, bob_token, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    for bad in (0, -5, 1.5, "100", True):
        expect(capture(api, bob_token, "a_1", {"amount": bad}), 422, "validation_failed")


def test_capture_above_remaining_is_refused(api, ada_token, bob_token, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    expect(capture(api, bob_token, "a_1", {"amount": 2001}), 422, "capture_exceeds_authorization")
    assert me(api, ada_token)["held"] == 2000  # nothing captured


def test_capture_forbidden_for_non_receiver(api, ada_token, bob_token, cy_token, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    expect(capture(api, ada_token, "a_1", {"amount": 100}), 403, "forbidden")  # payer, not receiver
    expect(capture(api, cy_token, "a_1", {"amount": 100}), 403, "forbidden")  # neither party


def test_capture_unknown_authorization_is_404(api, ada_token, bob_token):
    expect(capture(api, bob_token, "a_missing", {"amount": 100}), 404, "not_found")
    expect(capture(api, ada_token, "a_missing", {"amount": 100}), 404, "not_found")


def test_capture_expired_hold_reports_authorization_expired(api, bob_token, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_1", "u_ada", "u_bob", 2000, expires_at=LONG_AGO)
            ]
        )
    )
    expect(capture(api, bob_token, "a_1", {"amount": 100}), 409, "authorization_expired")


def test_capture_closed_hold_reports_not_open_before_amount_errors(api, ada_token, bob_token, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000, status="voided")]))
    # status precedence: a closed hold is 409 even with an otherwise-invalid amount.
    expect(capture(api, bob_token, "a_1", {"amount": 0}), 409, "authorization_not_open")
    expect(capture(api, bob_token, "a_1", {"amount": 99999}), 409, "authorization_not_open")


# --------------------------------------------------------------------------- #
# Void errors
# --------------------------------------------------------------------------- #


def test_void_forbidden_for_non_payer(api, ada_token, bob_token, cy_token, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    expect(api.post("/authorizations/a_1/void", token=bob_token), 403, "forbidden")
    expect(api.post("/authorizations/a_1/void", token=cy_token), 403, "forbidden")
    assert me(api, ada_token)["held"] == 2000  # still held


def test_void_unknown_authorization_is_404(api, ada_token):
    expect(api.post("/authorizations/a_missing/void", token=ada_token), 404, "not_found")


def test_void_captured_hold_is_not_open(api, ada_token, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000, status="captured")]))
    expect(api.post("/authorizations/a_1/void", token=ada_token), 409, "authorization_not_open")


def test_void_clock_expired_hold_is_not_open(api, ada_token, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_1", "u_ada", "u_bob", 2000, expires_at=LONG_AGO)
            ]
        )
    )
    expect(api.post("/authorizations/a_1/void", token=ada_token), 409, "authorization_not_open")


# --------------------------------------------------------------------------- #
# Capture idempotency
# --------------------------------------------------------------------------- #


def test_capture_replay_moves_money_once(api, ada_token, bob_token, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    key = unique("cap-replay")
    body = {"amount": 500, "final": False}
    first = capture(api, bob_token, "a_1", body, key=key)
    expect(first, 201)
    replay = capture(api, bob_token, "a_1", body, key=key)
    expect(replay, 200)
    assert json_equals(payload(replay), payload(first))
    assert me(api, bob_token)["total"] == 3000  # moved once
    assert me(api, ada_token)["held"] == 1500
    expect(capture(api, bob_token, "a_1", {"amount": 600, "final": False}, key=key), 409, "idempotency_key_reuse")


def test_capture_body_equality_is_json_value_equality(api, ada_token, bob_token, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    key = unique("cap-body")
    expect(capture(api, bob_token, "a_1", {}, key=key), 201)
    # {} and {"amount": N} are different JSON bodies even though they mean the same capture.
    expect(capture(api, bob_token, "a_1", {"amount": 2000}, key=key), 409, "idempotency_key_reuse")
