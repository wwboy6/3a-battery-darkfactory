"""Stage-3 stable statement pagination via snapshot tokens."""

import pytest

from support import (
    correction,
    expect,
    login,
    make_fixture,
    payload,
    seconds_from_now,
    seeded_payment,
    statement,
    unique,
    user,
)

T1 = "2026-09-10T12:00:00+00:00"


def snap_fixture():
    return make_fixture(
        users=[user("u_ada", "ada", 10000), user("u_bob", "bob", 2500)],
        payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, created_at=T1)],
    )


@pytest.fixture
def sf(api, reset):
    reset(snap_fixture())
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in ("ada", "bob")}


def test_first_statement_returns_a_snapshot_token(api, sf):
    body = payload(statement(api, sf["ada"]))
    assert isinstance(body["snapshot"], str) and body["snapshot"]


def test_snapshot_freezes_entries_across_later_payments(api, sf):
    first = payload(statement(api, sf["ada"]))
    token = first["snapshot"]
    api.post("/payments", token=sf["ada"], idem=unique("pay"), body={"to_handle": "bob", "amount": 40})

    fresh = payload(statement(api, sf["ada"]))
    assert len(fresh["entries"]) == 2  # the new payment is visible now
    paged = payload(statement(api, sf["ada"], snapshot=token))
    assert paged["entries"] == first["entries"]
    assert paged["opening_balance"] == first["opening_balance"]
    assert paged["closing_balance"] == first["closing_balance"]


def test_snapshot_freezes_selected_revision_across_corrections(api, sf):
    first = payload(statement(api, sf["ada"]))
    token = first["snapshot"]
    correction(
        api, sf["ada"], "p_1",
        expected_revision=1, amount=100, effective_at=seconds_from_now(-60), reason="smaller",
    )
    paged = payload(statement(api, sf["ada"], snapshot=token))
    assert paged["entries"] == first["entries"]
    assert paged["entries"][0]["revision"] == 1
    assert paged["entries"][0]["payment"]["amount"] == 500


def test_snapshot_paging_keeps_balances_and_has_more(api, sf):
    first = payload(statement(api, sf["ada"]))
    token = first["snapshot"]
    page = payload(statement(api, sf["ada"], snapshot=token, limit=1, offset=0))
    assert len(page["entries"]) == 1
    assert page["entries"][0]["balance_after"] == first["entries"][0]["balance_after"]
    assert page["opening_balance"] == first["opening_balance"]
    assert page["closing_balance"] == first["closing_balance"]
    assert page["has_more"] is False  # only one entry total


def test_snapshot_offset_beyond_end_is_empty(api, sf):
    token = payload(statement(api, sf["ada"]))["snapshot"]
    body = payload(statement(api, sf["ada"], snapshot=token, limit=1, offset=5))
    assert body["entries"] == []
    assert body["has_more"] is False
    assert body["opening_balance"] == 10500 and body["closing_balance"] == 10000


@pytest.mark.parametrize("param", [{"from": T1}, {"to": T1}, {"known_at": T1}])
def test_snapshot_rejects_window_params(api, sf, param):
    token = payload(statement(api, sf["ada"]))["snapshot"]
    expect(statement(api, sf["ada"], snapshot=token, **param), 422, "validation_failed")


def test_snapshot_unknown_token_is_404(api, sf):
    expect(statement(api, sf["ada"], snapshot="nope"), 404, "not_found")


def test_snapshot_foreign_token_is_404(api, sf):
    foreign = payload(statement(api, sf["bob"]))["snapshot"]
    expect(statement(api, sf["ada"], snapshot=foreign), 404, "not_found")


def test_snapshot_pre_reset_token_is_404(api, reset, sf):
    token = payload(statement(api, sf["ada"]))["snapshot"]
    reset(make_fixture())
    tok = payload(login(api, "ada@example.com"))["token"]
    expect(statement(api, tok, snapshot=token), 404, "not_found")
