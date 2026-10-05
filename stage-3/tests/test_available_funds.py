"""Stage-2: every 409 insufficient_funds is evaluated against `available`."""

from support import (
    authorize,
    capture,
    expect,
    login,
    make_fixture,
    me,
    payload,
    seeded_authorization,
    unique,
)


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def test_payment_uses_available_not_total(api, tokens):
    authorize(api, tokens["ada"], "bob", 2000)  # available 8000, total 10000
    resp = api.post(
        "/payments", token=tokens["ada"], idem=unique("pay"),
        body={"to_handle": "bob", "amount": 9000},
    )
    expect(resp, 409, "insufficient_funds")

    ok = api.post(
        "/payments", token=tokens["ada"], idem=unique("pay"),
        body={"to_handle": "bob", "amount": 8000},
    )
    expect(ok, 201)
    body = me(api, tokens["ada"])
    assert body["total"] == 2000 and body["held"] == 2000 and body["available"] == 0


def test_request_pay_uses_available_not_total(api, tokens):
    authorize(api, tokens["ada"], "bob", 2000)  # available 8000

    big = payload(
        api.post("/requests", token=tokens["bob"], idem=unique("req"),
                 body={"payer_handle": "ada", "amount": 9000})
    )["request_id"]
    expect(
        api.post(f"/requests/{big}/pay", token=tokens["ada"], idem=unique("pay"), body={}),
        409, "insufficient_funds",
    )

    ok = payload(
        api.post("/requests", token=tokens["bob"], idem=unique("req"),
                 body={"payer_handle": "ada", "amount": 8000})
    )["request_id"]
    expect(api.post(f"/requests/{ok}/pay", token=tokens["ada"], idem=unique("pay"), body={}), 201)

    body = me(api, tokens["ada"])
    assert body["total"] == 2000 and body["held"] == 2000 and body["available"] == 0


def test_settlement_affordability_uses_available(api, reset):
    reset(
        make_fixture(
            settlement_operator_ids=["u_ada"],
            authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)],
        )
    )
    tok = relogin(api, "ada", "bob", "cy")
    too_big = {"transfers": [{"from_handle": "ada", "to_handle": "cy", "amount": 9000}]}
    expect(api.post("/settlements", token=tok["ada"], idem=unique("stl"), body=too_big), 409, "insufficient_funds")

    ok = {"transfers": [{"from_handle": "ada", "to_handle": "cy", "amount": 8000}]}
    expect(api.post("/settlements", token=tok["ada"], idem=unique("stl"), body=ok), 201)
    body = me(api, tok["ada"])
    assert body["total"] == 2000 and body["held"] == 2000 and body["available"] == 0


def test_settlement_net_incoming_can_cover_available(api, reset):
    """Affordability is net: available + incoming - outgoing >= 0."""
    reset(
        make_fixture(
            settlement_operator_ids=["u_ada"],
            authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 6000)],
        )
    )
    tok = relogin(api, "ada", "bob", "cy")  # ada available 4000
    transfers = {
        "transfers": [
            {"from_handle": "ada", "to_handle": "bob", "amount": 9000},
            {"from_handle": "bob", "to_handle": "ada", "amount": 5000},
        ]
    }
    expect(api.post("/settlements", token=tok["ada"], idem=unique("stl"), body=transfers), 201)
    ada = me(api, tok["ada"])
    assert ada["total"] == 6000 and ada["held"] == 6000 and ada["available"] == 0


def test_held_funds_cannot_fund_a_payment_but_capture_can_spend_them(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 5000))["authorization_id"]  # available 5000
    expect(
        api.post("/payments", token=tokens["ada"], idem=unique("pay"),
                 body={"to_handle": "cy", "amount": 6000}),
        409, "insufficient_funds",
    )
    expect(capture(api, tokens["bob"], aid), 201)  # capture spends the reserved 5000
    body = me(api, tokens["ada"])
    assert body["total"] == 5000 and body["held"] == 0 and body["available"] == 5000
