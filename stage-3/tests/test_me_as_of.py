"""Stage-3 GET /me?as_of: historical balance."""

import pytest

from support import (
    expect,
    get_me,
    login,
    make_fixture,
    me,
    payload,
    seeded_payment,
    seconds_from_now,
)

T1 = "2026-09-10T12:00:00+00:00"
BEFORE = "2026-09-10T11:59:59+00:00"


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def history_fixture():
    return make_fixture(payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, created_at=T1)])


def test_me_without_params_has_no_temporal_keys(api, tokens):
    body = me(api, tokens["ada"])
    assert "as_of" not in body and "known_at" not in body
    assert body["balance"] == 10000


def test_me_as_of_after_latest_payment_is_current(api, reset):
    reset(history_fixture())
    tok = relogin(api, "ada")["ada"]
    body = payload(get_me(api, tok, as_of=seconds_from_now(3600)))
    assert body["balance"] == 10000


def test_me_as_of_before_first_payment_is_opening_balance(api, reset):
    reset(history_fixture())
    tok = relogin(api, "ada")["ada"]
    body = payload(get_me(api, tok, as_of=BEFORE))
    # seeded balance 10000 is after sending 500, so opening is 10500
    assert body["balance"] == 10500


def test_me_as_of_exactly_at_payment_counts(api, reset):
    reset(history_fixture())
    tok = relogin(api, "ada")["ada"]
    body = payload(get_me(api, tok, as_of=T1))
    assert body["balance"] == 10000  # the payment at exactly as_of has happened


def test_me_as_of_echoed_exactly(api, tokens):
    value = "2026-09-10T06:30:00+02:00"
    body = payload(get_me(api, tokens["ada"], as_of=value))
    assert body["as_of"] == value


def test_me_as_of_receiver_opening_balance(api, reset):
    reset(history_fixture())
    tok = relogin(api, "bob")["bob"]
    before = payload(get_me(api, tok, as_of=BEFORE))
    after = payload(get_me(api, tok, as_of=seconds_from_now(3600)))
    assert before["balance"] == 2000  # 2500 after receiving 500
    assert after["balance"] == 2500


@pytest.mark.parametrize(
    "value",
    ["2026-09-24", "2026-09-24T13:20:00", "", "not-a-time", "13:20:00+00:00", "2026-09-24T13:20:00+25:00"],
)
def test_me_invalid_as_of_is_422(api, tokens, value):
    expect(get_me(api, tokens["ada"], as_of=value), 422, "validation_failed")


@pytest.mark.parametrize(
    "value",
    ["2026-09-24", "2026-09-24T13:20:00", "", "not-a-time"],
)
def test_me_invalid_known_at_is_422(api, tokens, value):
    expect(get_me(api, tokens["ada"], known_at=value), 422, "validation_failed")
