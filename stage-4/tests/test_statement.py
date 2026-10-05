"""Stage-3 GET /statement: window, ordering and pagination-independent balances."""

import pytest

from support import (
    expect,
    login,
    make_fixture,
    payload,
    seeded_payment,
    statement,
    user,
)

T1 = "2026-09-10T12:00:00+00:00"
T2 = "2026-09-11T12:00:00+00:00"
T3 = "2026-09-12T12:00:00+00:00"
T4 = "2026-09-13T12:00:00+00:00"


def statement_fixture():
    return make_fixture(
        users=[user("u_ada", "ada", 10000), user("u_bob", "bob", 2500), user("u_cy", "cy", 150)],
        payments=[
            seeded_payment("p_1", "u_ada", "u_bob", 500, created_at=T1),
            seeded_payment("p_2", "u_bob", "u_ada", 200, created_at=T2),
            seeded_payment("p_3", "u_ada", "u_cy", 100, created_at=T3),
            seeded_payment("p_4", "u_bob", "u_cy", 50, created_at=T4),
        ],
    )


@pytest.fixture
def st(api, reset):
    reset(statement_fixture())
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in ("ada", "bob", "cy")}


def entries_of(body):
    return body["entries"]


def test_statement_full_window(api, st):
    body = payload(statement(api, st["ada"]))
    assert body["opening_balance"] == 10400
    assert body["closing_balance"] == 10000
    assert [e["payment"]["payment_id"] for e in entries_of(body)] == ["p_1", "p_2", "p_3"]
    assert [e["delta"] for e in entries_of(body)] == [-500, 200, -100]
    assert [e["balance_after"] for e in entries_of(body)] == [9900, 10100, 10000]
    assert body["has_more"] is False
    assert "snapshot" in body
    assert body["opening_balance"] + sum(e["delta"] for e in entries_of(body)) == body["closing_balance"]


def test_statement_half_open_window(api, st):
    body = payload(statement(api, st["ada"], **{"from": T2, "to": T3}))
    assert [e["payment"]["payment_id"] for e in entries_of(body)] == ["p_2"]
    assert body["opening_balance"] == 9900  # balance immediately before T2
    assert body["closing_balance"] == 10100  # balance immediately before T3
    assert entries_of(body)[0]["balance_after"] == 10100


def test_statement_window_before_first_payment_is_empty(api, st):
    body = payload(statement(api, st["ada"], **{"to": "2026-01-01T00:00:00+00:00"}))
    assert body["entries"] == []
    assert body["opening_balance"] == body["closing_balance"] == 10400


def test_statement_excludes_third_party_payments(api, st):
    ada_ids = [e["payment"]["payment_id"] for e in entries_of(payload(statement(api, st["ada"])))]
    assert "p_4" not in ada_ids  # bob -> cy is public but not ada's
    bob_ids = [e["payment"]["payment_id"] for e in entries_of(payload(statement(api, st["bob"])))]
    assert bob_ids == ["p_1", "p_2", "p_4"]
    cy_ids = [e["payment"]["payment_id"] for e in entries_of(payload(statement(api, st["cy"])))]
    assert cy_ids == ["p_3", "p_4"]


def test_statement_pagination_is_pagination_independent(api, st):
    full = payload(statement(api, st["ada"]))
    page1 = payload(statement(api, st["ada"], limit=1, offset=0))
    page2 = payload(statement(api, st["ada"], limit=1, offset=1))
    page3 = payload(statement(api, st["ada"], limit=1, offset=2))

    assert page1["opening_balance"] == full["opening_balance"] == 10400
    assert page1["closing_balance"] == full["closing_balance"] == 10000
    assert page1["entries"][0]["payment"]["payment_id"] == "p_1"
    assert page1["entries"][0]["balance_after"] == 9900
    assert page1["has_more"] is True
    assert page2["entries"][0]["balance_after"] == 10100 and page2["has_more"] is True
    assert page3["entries"][0]["balance_after"] == 10000 and page3["has_more"] is False


def test_statement_offset_beyond_end(api, st):
    body = payload(statement(api, st["ada"], limit=2, offset=10))
    assert body["entries"] == []
    assert body["has_more"] is False
    assert body["opening_balance"] == 10400 and body["closing_balance"] == 10000


def test_statement_ties_ordered_by_payment_id(api, reset):
    reset(
        make_fixture(
            users=[user("u_ada", "ada", 970), user("u_bob", "bob", 2530)],
            payments=[
                seeded_payment("t_b", "u_ada", "u_bob", 10, created_at=T1),
                seeded_payment("t_a", "u_ada", "u_bob", 20, created_at=T1),
            ],
        )
    )
    tok = payload(login(api, "ada@example.com"))["token"]
    body = payload(statement(api, tok))
    assert [e["payment"]["payment_id"] for e in entries_of(body)] == ["t_a", "t_b"]
    assert [e["delta"] for e in entries_of(body)] == [-20, -10]
    assert [e["balance_after"] for e in entries_of(body)] == [980, 970]


@pytest.mark.parametrize("value", ["2026-09-24", "2026-09-24T13:20:00", "", "nope"])
def test_statement_invalid_from_is_422(api, st, value):
    expect(statement(api, st["ada"], **{"from": value}), 422, "validation_failed")


@pytest.mark.parametrize("value", ["2026-09-24", "2026-09-24T13:20:00", "", "nope"])
def test_statement_invalid_to_is_422(api, st, value):
    expect(statement(api, st["ada"], **{"to": value}), 422, "validation_failed")


@pytest.mark.parametrize("value", ["0", "201", "1e9", "-1"])
def test_statement_invalid_limit_is_422(api, st, value):
    expect(statement(api, st["ada"], limit=value), 422, "validation_failed")
