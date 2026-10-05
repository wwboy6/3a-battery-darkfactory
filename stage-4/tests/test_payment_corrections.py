"""Stage-3 POST /payments/{id}/corrections: money math, safety and immutability."""

import datetime
import time

import pytest

from support import (
    assert_rfc3339,
    authorize,
    capture,
    correction,
    expect,
    login,
    make_fixture,
    me,
    payload,
    revisions,
    seconds_from_now,
    seeded_payment,
    statement,
    unique,
    user,
)

PAST = "2026-09-10T12:00:00+00:00"


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


@pytest.fixture
def pay500(api, tokens):
    resp = api.post("/payments", token=tokens["ada"], idem=unique("pay"), body={"to_handle": "bob", "amount": 500})
    expect(resp, 201)
    return payload(resp)["payment_id"]


def rev_count(api, tokens, pid):
    items = payload(revisions(api, tokens["ada"], pid))["revisions"]
    return len(items)


# --------------------------------------------------------------------------- #
# Happy path / money math
# --------------------------------------------------------------------------- #


def test_correction_response_shape(api, tokens, pay500):
    resp = correction(
        api, tokens["ada"], pay500,
        expected_revision=1, amount=300, effective_at=seconds_from_now(-60), reason="fix",
    )
    expect(resp, 201)
    body = payload(resp)
    assert set(body) == {"payment_id", "revision", "amount", "effective_at", "recorded_at", "reason"}
    assert body["payment_id"] == pay500
    assert body["revision"] == 2
    assert body["amount"] == 300
    assert body["reason"] == "fix"
    assert_rfc3339(body["effective_at"], "effective_at")
    assert_rfc3339(body["recorded_at"], "recorded_at")


def test_decrease_debits_the_receiver(api, tokens, pay500):
    correction(api, tokens["ada"], pay500, expected_revision=1, amount=300, effective_at=seconds_from_now(-60), reason="less")
    assert me(api, tokens["ada"])["balance"] == 9700  # 9500 + 200
    assert me(api, tokens["bob"])["balance"] == 2800  # 3000 - 200


def test_increase_debits_the_sender(api, tokens, pay500):
    correction(api, tokens["ada"], pay500, expected_revision=1, amount=800, effective_at=seconds_from_now(-60), reason="more")
    assert me(api, tokens["ada"])["balance"] == 9200  # 9500 - 300
    assert me(api, tokens["bob"])["balance"] == 3300  # 3000 + 300


def test_zero_amount_reverses_the_payment(api, tokens, pay500):
    correction(api, tokens["ada"], pay500, expected_revision=1, amount=0, effective_at=seconds_from_now(-60), reason="reverse")
    assert me(api, tokens["ada"])["balance"] == 10000
    assert me(api, tokens["bob"])["balance"] == 2500


def test_correction_preserves_total(api, tokens, pay500):
    correction(api, tokens["ada"], pay500, expected_revision=1, amount=123, effective_at=seconds_from_now(-60), reason="x")
    total = sum(me(api, tokens[h])["balance"] for h in ("ada", "bob", "cy"))
    assert total == 12500


def test_correction_keeps_original_payment_in_feed(api, tokens, pay500):
    correction(api, tokens["ada"], pay500, expected_revision=1, amount=300, effective_at=seconds_from_now(-60), reason="x")
    entry = next(
        p for p in payload(api.get("/activity", token=tokens["ada"]))["payments"] if p["payment_id"] == pay500
    )
    assert entry["amount"] == 500  # the feed keeps the original receipt


def test_statement_uses_the_selected_revision(api, tokens, pay500):
    correction(api, tokens["ada"], pay500, expected_revision=1, amount=300, effective_at=seconds_from_now(-60), reason="x")
    body = payload(statement(api, tokens["ada"]))
    entry = next(e for e in body["entries"] if e["payment"]["payment_id"] == pay500)
    assert entry["payment"]["amount"] == 300
    assert entry["delta"] == -300
    assert entry["revision"] == 2


# --------------------------------------------------------------------------- #
# Authorisation / lookup
# --------------------------------------------------------------------------- #


def test_correction_without_token_is_401(api, tokens, pay500):
    body = {"expected_revision": 1, "amount": 300, "effective_at": seconds_from_now(-60), "reason": "x"}
    expect(api.post(f"/payments/{pay500}/corrections", idem=unique("corr"), body=body), 401, "unauthenticated")


def test_receiver_cannot_correct(api, tokens, pay500):
    expect(
        correction(api, tokens["bob"], pay500, expected_revision=1, amount=300, effective_at=seconds_from_now(-60), reason="x"),
        403, "forbidden",
    )


def test_third_party_cannot_correct(api, tokens, pay500):
    expect(
        correction(api, tokens["cy"], pay500, expected_revision=1, amount=300, effective_at=seconds_from_now(-60), reason="x"),
        403, "forbidden",
    )


def test_correction_unknown_payment_is_404(api, tokens):
    expect(
        correction(api, tokens["ada"], "p_missing", expected_revision=1, amount=100, effective_at=seconds_from_now(-60), reason="x"),
        404, "not_found",
    )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("missing", ["expected_revision", "amount", "effective_at", "reason"])
def test_missing_required_field_is_422(api, tokens, pay500, missing):
    body = {"expected_revision": 1, "amount": 300, "effective_at": seconds_from_now(-60), "reason": "x"}
    del body[missing]
    expect(api.post(f"/payments/{pay500}/corrections", token=tokens["ada"], idem=unique("corr"), body=body), 422, "validation_failed")


@pytest.mark.parametrize("amount", [-1, 1000000001, 1.5, "100", True, None])
def test_invalid_amount_is_422(api, tokens, pay500, amount):
    expect(
        correction(api, tokens["ada"], pay500, expected_revision=1, amount=amount, effective_at=seconds_from_now(-60), reason="x"),
        422, "validation_failed",
    )


@pytest.mark.parametrize("reason", ["", "x" * 201, 12, None])
def test_invalid_reason_is_422(api, tokens, pay500, reason):
    expect(
        correction(api, tokens["ada"], pay500, expected_revision=1, amount=300, effective_at=seconds_from_now(-60), reason=reason),
        422, "validation_failed",
    )


@pytest.mark.parametrize("value", ["2026-09-24", "2026-09-24T13:20:00", "", "nope"])
def test_invalid_effective_at_is_422(api, tokens, pay500, value):
    expect(
        correction(api, tokens["ada"], pay500, expected_revision=1, amount=300, effective_at=value, reason="x"),
        422, "validation_failed",
    )


def test_effective_at_in_the_future_is_422(api, tokens, pay500):
    expect(
        correction(api, tokens["ada"], pay500, expected_revision=1, amount=300, effective_at=seconds_from_now(3600), reason="x"),
        422, "validation_failed",
    )


@pytest.mark.parametrize("value", [0, -1, 1.5, "1", None])
def test_invalid_expected_revision_is_422(api, tokens, pay500, value):
    expect(
        correction(api, tokens["ada"], pay500, expected_revision=value, amount=300, effective_at=seconds_from_now(-60), reason="x"),
        422, "validation_failed",
    )


# --------------------------------------------------------------------------- #
# Staleness / idempotency
# --------------------------------------------------------------------------- #


def test_stale_expected_revision_is_409(api, tokens, pay500):
    correction(api, tokens["ada"], pay500, expected_revision=1, amount=300, effective_at=seconds_from_now(-60), reason="first")
    expect(
        correction(api, tokens["ada"], pay500, expected_revision=1, amount=200, effective_at=seconds_from_now(-30), reason="stale"),
        409, "stale_revision",
    )


def test_replay_returns_original_revision_after_newer_ones(api, tokens, pay500):
    key = unique("corr")
    body = dict(expected_revision=1, amount=300, effective_at=seconds_from_now(-120), reason="first")
    first = correction(api, tokens["ada"], pay500, key=key, **body)
    expect(first, 201)
    expect(
        correction(api, tokens["ada"], pay500, expected_revision=2, amount=200, effective_at=seconds_from_now(-60), reason="second"),
        201,
    )
    replay = correction(api, tokens["ada"], pay500, key=key, **body)
    expect(replay, 200)
    assert payload(replay) == payload(first)


def test_correction_idempotency_reuse_is_409(api, tokens, pay500):
    key = unique("corr")
    expect(correction(api, tokens["ada"], pay500, key=key, expected_revision=1, amount=300, effective_at=seconds_from_now(-60), reason="x"), 201)
    expect(correction(api, tokens["ada"], pay500, key=key, expected_revision=1, amount=250, effective_at=seconds_from_now(-60), reason="x"), 409, "idempotency_key_reuse")


def test_correction_missing_idempotency_key_is_400(api, tokens, pay500):
    body = {"expected_revision": 1, "amount": 300, "effective_at": seconds_from_now(-60), "reason": "x"}
    expect(api.post(f"/payments/{pay500}/corrections", token=tokens["ada"], body=body), 400, "missing_idempotency_key")


def test_recorded_at_strictly_increases(api, tokens, pay500):
    r2 = payload(correction(api, tokens["ada"], pay500, expected_revision=1, amount=450, effective_at=seconds_from_now(-90), reason="a"))
    time.sleep(1.1)
    r3 = payload(correction(api, tokens["ada"], pay500, expected_revision=2, amount=400, effective_at=seconds_from_now(-30), reason="b"))
    assert datetime.datetime.fromisoformat(r3["recorded_at"]) > datetime.datetime.fromisoformat(r2["recorded_at"])


# --------------------------------------------------------------------------- #
# Affordability / overdraft
# --------------------------------------------------------------------------- #


def test_current_insufficient_funds_is_409(api, tokens, pay500):
    expect(
        correction(api, tokens["ada"], pay500, expected_revision=1, amount=1000000000, effective_at=seconds_from_now(-60), reason="huge"),
        409, "insufficient_funds",
    )
    assert rev_count(api, tokens, pay500) == 1
    assert me(api, tokens["ada"])["balance"] == 9500
    assert me(api, tokens["bob"])["balance"] == 3000


def test_historical_overdraft_is_409(api, reset):
    reset(
        make_fixture(
            users=[user("u_ada", "ada", 1000), user("u_bob", "bob", 2500)],
            payments=[
                seeded_payment("p_1", "u_ada", "u_bob", 500, created_at="2026-09-10T12:00:00+00:00"),
                seeded_payment("p_2", "u_bob", "u_ada", 500, created_at="2026-09-11T12:00:00+00:00"),
            ],
        )
    )
    tok = relogin(api, "ada", "bob")
    # ada opening 1000 -> 500 -> 1000; increasing p_1 to 1200 dips the past below zero.
    resp = correction(api, tok["ada"], "p_1", expected_revision=1, amount=1200, effective_at="2026-09-10T11:00:00+00:00", reason="too big")
    expect(resp, 409, "historical_overdraft")
    assert me(api, tok["ada"])["balance"] == 1000
    assert me(api, tok["bob"])["balance"] == 2500


def test_insufficient_funds_precedes_historical_overdraft(api, reset):
    reset(
        make_fixture(
            users=[user("u_ada", "ada", 1000), user("u_bob", "bob", 2500)],
            payments=[
                seeded_payment("p_1", "u_ada", "u_bob", 500, created_at="2026-09-10T12:00:00+00:00"),
                seeded_payment("p_2", "u_bob", "u_ada", 500, created_at="2026-09-11T12:00:00+00:00"),
            ],
        )
    )
    tok = relogin(api, "ada", "bob")
    # Currently unaffordable, so insufficient_funds wins over historical_overdraft.
    resp = correction(api, tok["ada"], "p_1", expected_revision=1, amount=1000000000, effective_at="2026-09-10T11:00:00+00:00", reason="huge")
    expect(resp, 409, "insufficient_funds")


def test_failed_correction_leaves_state_untouched(api, tokens, pay500):
    before_feed = payload(api.get("/activity", token=tokens["ada"]))["payments"]
    expect(
        correction(api, tokens["ada"], pay500, expected_revision=1, amount=1000000000, effective_at=seconds_from_now(-60), reason="huge"),
        409, "insufficient_funds",
    )
    assert rev_count(api, tokens, pay500) == 1
    assert payload(api.get("/activity", token=tokens["ada"]))["payments"] == before_feed
    assert me(api, tokens["ada"])["balance"] == 9500
    assert me(api, tokens["bob"])["balance"] == 3000


# --------------------------------------------------------------------------- #
# Immutable linked payments
# --------------------------------------------------------------------------- #


def test_correction_of_settlement_member_is_422(api, reset):
    reset(make_fixture(settlement_operator_ids=["u_ada"]))
    tok = relogin(api, "ada", "bob")
    settle = api.post(
        "/settlements", token=tok["ada"], idem=unique("stl"),
        body={"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 100}]},
    )
    expect(settle, 201)
    pid = payload(settle)["payments"][0]["payment_id"]
    expect(
        correction(api, tok["ada"], pid, expected_revision=1, amount=50, effective_at=seconds_from_now(-60), reason="x"),
        422, "linked_payment_immutable",
    )


def test_correction_of_capture_is_422(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 1000))["authorization_id"]
    payment = payload(capture(api, tokens["bob"], aid))
    expect(
        correction(api, tokens["ada"], payment["payment_id"], expected_revision=1, amount=500, effective_at=seconds_from_now(-60), reason="x"),
        422, "linked_payment_immutable",
    )


def test_correction_does_not_change_visibility(api, tokens):
    pid = payload(
        api.post("/payments", token=tokens["ada"], idem=unique("pay"), body={"to_handle": "bob", "amount": 500, "visibility": "private"})
    )["payment_id"]
    correction(api, tokens["ada"], pid, expected_revision=1, amount=300, effective_at=seconds_from_now(-60), reason="x")
    for handle in ("ada", "bob"):
        assert pid in [p["payment_id"] for p in payload(api.get("/activity", token=tokens[handle]))["payments"]]
    assert pid not in [p["payment_id"] for p in payload(api.get("/activity", token=tokens["cy"]))["payments"]]
