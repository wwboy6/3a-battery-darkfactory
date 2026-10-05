"""Stage-2: /me total / available / held derivation from authorization holds."""

from support import (
    authorize,
    capture,
    login,
    make_fixture,
    me,
    payload,
    seeded_authorization,
    void_authorization,
)


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def test_me_without_holds_balance_total_available_agree(api, tokens):
    body = me(api, tokens["ada"])
    assert body["balance"] == body["total"] == body["available"] == 10000
    assert body["held"] == 0


def test_me_seeded_open_hold_reduces_available_only(api, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    token = relogin(api, "ada")["ada"]
    body = me(api, token)
    assert body["balance"] == 10000 and body["total"] == 10000
    assert body["held"] == 2000
    assert body["available"] == 8000


def test_me_multiple_open_holds_sum_ignoring_closed(api, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_1", "u_ada", "u_bob", 1000),
                seeded_authorization("a_2", "u_ada", "u_cy", 500),
                seeded_authorization("a_3", "u_ada", "u_bob", 700, status="captured"),
                seeded_authorization("a_4", "u_ada", "u_bob", 400, status="voided"),
                seeded_authorization("a_5", "u_ada", "u_bob", 300, status="expired"),
            ]
        )
    )
    body = me(api, relogin(api, "ada")["ada"])
    assert body["held"] == 1500
    assert body["available"] == 8500


def test_me_new_hold_then_release(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    mid = me(api, tokens["ada"])
    assert mid["held"] == 2000 and mid["available"] == 8000 and mid["total"] == 10000

    void_authorization(api, tokens["ada"], aid)
    after = me(api, tokens["ada"])
    assert after["held"] == 0 and after["available"] == 10000


def test_me_available_can_reach_zero_but_not_negative(api, tokens):
    payload(authorize(api, tokens["ada"], "bob", 10000))
    body = me(api, tokens["ada"])
    assert body["held"] == 10000
    assert body["available"] == 0
    assert body["total"] == 10000


def test_me_reflects_partial_capture_remainder(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    capture(api, tokens["bob"], aid, amount=1500, final=False)
    body = me(api, tokens["ada"])
    assert body["total"] == 8500  # 1500 moved
    assert body["held"] == 500  # remainder still reserved
    assert body["available"] == 8000


def test_me_held_zero_after_full_capture(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    capture(api, tokens["bob"], aid)
    body = me(api, tokens["ada"])
    assert body["total"] == 8000 and body["held"] == 0 and body["available"] == 8000
    assert me(api, tokens["bob"])["total"] == 4500


def test_me_derived_fields_are_integers(api, tokens):
    body = me(api, tokens["ada"])
    for field in ("total", "available", "held", "balance"):
        assert isinstance(body[field], int) and not isinstance(body[field], bool)
