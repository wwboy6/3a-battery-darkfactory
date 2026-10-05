"""Stage-3 GET /payments/{id}/revisions."""

from support import (
    assert_rfc3339,
    correction,
    expect,
    login,
    make_fixture,
    payload,
    revisions,
    seeded_payment,
    seconds_from_now,
    unique,
)

T1 = "2026-09-10T12:00:00+00:00"


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def make_payment(api, token, amount=500):
    resp = api.post("/payments", token=token, idem=unique("pay"), body={"to_handle": "bob", "amount": amount})
    expect(resp, 201)
    return payload(resp)


def revs(api, token, pid):
    resp = revisions(api, token, pid)
    expect(resp, 200)
    return payload(resp)["revisions"]


def test_revision_1_matches_the_original_receipt(api, tokens):
    payment = make_payment(api, tokens["ada"], 500)
    items = revs(api, tokens["ada"], payment["payment_id"])
    assert len(items) == 1
    first = items[0]
    assert first["revision"] == 1
    assert first["amount"] == 500
    assert first["effective_at"] == payment["created_at"]
    assert first["recorded_at"] == payment["created_at"]
    assert first["reason"] == ""


def test_correction_appends_revision_in_order(api, tokens):
    payment = make_payment(api, tokens["ada"], 500)
    pid = payment["payment_id"]
    first = payload(
        correction(
            api, tokens["ada"], pid,
            expected_revision=1, amount=300, effective_at=seconds_from_now(-60), reason="fix",
        )
    )
    expect(revisions(api, tokens["ada"], pid), 200)
    items = revs(api, tokens["ada"], pid)
    assert [r["revision"] for r in items] == [1, 2]
    assert items[1]["revision"] == first["revision"]
    assert items[1]["amount"] == 300
    assert items[1]["reason"] == "fix"
    assert items[1]["recorded_at"] == first["recorded_at"]


def test_revisions_visible_only_to_the_two_parties(api, tokens):
    payment = make_payment(api, tokens["ada"], 500)
    pid = payment["payment_id"]
    expect(revisions(api, tokens["ada"], pid), 200)
    expect(revisions(api, tokens["bob"], pid), 200)
    expect(revisions(api, tokens["cy"], pid), 404, "not_found")


def test_revisions_third_party_404_even_for_public_payment(api, tokens):
    payment = make_payment(api, tokens["ada"], 500)  # default visibility public
    expect(revisions(api, tokens["cy"], payment["payment_id"]), 404, "not_found")


def test_revisions_without_token_is_401(api, tokens):
    payment = make_payment(api, tokens["ada"], 500)
    expect(api.get(f"/payments/{payment['payment_id']}/revisions"), 401, "unauthenticated")


def test_revisions_unknown_payment_is_404(api, tokens):
    expect(revisions(api, tokens["ada"], "p_missing"), 404, "not_found")


def test_seeded_revision_1_uses_seeded_created_at(api, reset):
    reset(make_fixture(payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, created_at=T1)]))
    tok = relogin(api, "ada")["ada"]
    items = revs(api, tok, "p_1")
    assert len(items) == 1
    assert items[0]["revision"] == 1
    assert items[0]["effective_at"] == T1
    assert items[0]["recorded_at"] == T1
    assert_rfc3339(items[0]["recorded_at"])
