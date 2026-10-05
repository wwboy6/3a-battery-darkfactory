"""Stage-2 §7: authorizations and captures are idempotent write paths 6 and 7."""

from support import (
    authorize,
    capture,
    expect,
    login,
    me,
    payload,
    unique,
)


def test_authorization_create_requires_key(api, tokens):
    resp = api.post("/authorizations", token=tokens["ada"], body={"to_handle": "bob", "amount": 100})
    expect(resp, 400, "missing_idempotency_key")


def test_capture_requires_key(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 1000))["authorization_id"]
    resp = api.post(f"/authorizations/{aid}/capture", token=tokens["bob"], body={})
    expect(resp, 400, "missing_idempotency_key")


def test_authorization_create_replay_is_200_and_holds_once(api, tokens):
    key = unique("idem")
    body = {"to_handle": "bob", "amount": 2000}
    first = api.post("/authorizations", token=tokens["ada"], idem=key, body=body)
    expect(first, 201)
    replay = api.post("/authorizations", token=tokens["ada"], idem=key, body=body)
    expect(replay, 200)
    assert payload(replay) == payload(first)
    assert me(api, tokens["ada"])["held"] == 2000


def test_authorization_create_key_reuse_different_body_is_409(api, tokens):
    key = unique("idem")
    expect(api.post("/authorizations", token=tokens["ada"], idem=key, body={"to_handle": "bob", "amount": 100}), 201)
    expect(
        api.post("/authorizations", token=tokens["ada"], idem=key, body={"to_handle": "bob", "amount": 200}),
        409, "idempotency_key_reuse",
    )


def test_capture_replay_returns_original_payment_and_moves_money_once(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    key = unique("idem")
    first = api.post(f"/authorizations/{aid}/capture", token=tokens["bob"], idem=key, body={})
    expect(first, 201)
    replay = api.post(f"/authorizations/{aid}/capture", token=tokens["bob"], idem=key, body={})
    expect(replay, 200)
    assert payload(replay) == payload(first)
    assert me(api, tokens["ada"])["total"] == 8000


def test_capture_replay_after_closed_is_200_not_conflict(api, tokens):
    """The request is closed after the first capture, but a replay still wins."""
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    key = unique("idem")
    expect(api.post(f"/authorizations/{aid}/capture", token=tokens["bob"], idem=key, body={}), 201)
    replay = api.post(f"/authorizations/{aid}/capture", token=tokens["bob"], idem=key, body={})
    expect(replay, 200)


def test_capture_body_equality_empty_vs_explicit_amount(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    key = unique("idem")
    expect(api.post(f"/authorizations/{aid}/capture", token=tokens["bob"], idem=key, body={}), 201)
    expect(
        api.post(f"/authorizations/{aid}/capture", token=tokens["bob"], idem=key, body={"amount": 2000}),
        409, "idempotency_key_reuse",
    )


def test_capture_final_flag_is_part_of_body_equality(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    key = unique("idem")
    expect(
        api.post(f"/authorizations/{aid}/capture", token=tokens["bob"], idem=key, body={"amount": 700, "final": False}),
        201,
    )
    expect(
        api.post(f"/authorizations/{aid}/capture", token=tokens["bob"], idem=key, body={"amount": 700, "final": True}),
        409, "idempotency_key_reuse",
    )


def test_capture_key_reusable_after_4xx(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    key = unique("idem")
    expect(
        api.post(f"/authorizations/{aid}/capture", token=tokens["bob"], idem=key, body={"amount": 3000}),
        422, "capture_exceeds_authorization",
    )
    expect(api.post(f"/authorizations/{aid}/capture", token=tokens["bob"], idem=key, body={}), 201)


def test_authorization_key_scoped_per_user(api, tokens):
    key = unique("idem")
    expect(api.post("/authorizations", token=tokens["ada"], idem=key, body={"to_handle": "bob", "amount": 100}), 201)
    expect(api.post("/authorizations", token=tokens["bob"], idem=key, body={"to_handle": "cy", "amount": 100}), 201)
