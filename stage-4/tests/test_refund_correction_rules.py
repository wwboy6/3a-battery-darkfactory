"""Stage-4: corrections tightened by refunds (immutability and the refund floor)."""

from support import (
    authorize,
    capture,
    correction,
    create_payment,
    expect,
    login,
    me,
    payload,
    refund,
    seconds_from_now,
)


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def pay(api, tokens, amount=500):
    resp = create_payment(api, tokens["ada"], "bob", amount)
    expect(resp, 201)
    return payload(resp)


def correct(api, token, pid, amount):
    return correction(
        api, token, pid,
        expected_revision=1, amount=amount, effective_at=seconds_from_now(-60), reason="adjust",
    )


def test_refund_payment_cannot_be_corrected(api, tokens):
    original = pay(api, tokens, 500)
    refunded = payload(refund(api, tokens["bob"], original["payment_id"], amount=200))
    # bob is the refund's sender, so this reaches the immutability rule.
    expect(correct(api, tokens["bob"], refunded["payment_id"], 100), 422, "linked_payment_immutable")


def test_non_sender_correction_of_refund_is_403(api, tokens):
    original = pay(api, tokens, 500)
    refunded = payload(refund(api, tokens["bob"], original["payment_id"], amount=200))
    expect(correct(api, tokens["ada"], refunded["payment_id"], 100), 403, "forbidden")


def test_capture_payment_cannot_be_corrected(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 1000))["authorization_id"]
    captured = payload(capture(api, tokens["bob"], aid, amount=500, final=False))
    expect(correct(api, tokens["ada"], captured["payment_id"], 300), 422, "linked_payment_immutable")


def test_correction_below_refunded_amount_is_422(api, tokens):
    original = pay(api, tokens, 500)
    refund(api, tokens["bob"], original["payment_id"], amount=200)
    expect(correct(api, tokens["ada"], original["payment_id"], 100), 422, "refund_exceeds_payment")
    assert me(api, tokens["ada"])["balance"] == 9700
    assert me(api, tokens["bob"])["balance"] == 2800


def test_correction_down_to_refunded_amount_is_allowed(api, tokens):
    original = pay(api, tokens, 500)
    refund(api, tokens["bob"], original["payment_id"], amount=200)
    expect(correct(api, tokens["ada"], original["payment_id"], 200), 201)
    assert me(api, tokens["ada"])["balance"] == 10000
    assert me(api, tokens["bob"])["balance"] == 2500


def test_correction_above_refunded_amount_is_allowed(api, tokens):
    original = pay(api, tokens, 500)
    refund(api, tokens["bob"], original["payment_id"], amount=200)
    expect(correct(api, tokens["ada"], original["payment_id"], 600), 201)
    assert me(api, tokens["ada"])["balance"] == 9600
    assert me(api, tokens["bob"])["balance"] == 2900
