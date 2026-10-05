"""Stage-3: payment created_at timestamps and fixture seeding."""

from support import (
    assert_rfc3339,
    expect,
    login,
    make_fixture,
    me,
    payload,
    seeded_payment,
    seconds_from_now,
)


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def test_seeded_payment_created_at_is_returned(api, reset):
    ts = "2026-09-24T11:04:03+00:00"
    reset(make_fixture(payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, created_at=ts)]))
    tok = relogin(api, "ada")
    feed = payload(api.get("/activity", token=tok["ada"]))["payments"]
    assert feed[0]["created_at"] == ts


def test_seeded_payment_without_created_at_uses_reset_time(api, reset):
    reset(make_fixture(payments=[seeded_payment("p_1", "u_ada", "u_bob", 500)]))
    tok = relogin(api, "ada")
    payment = payload(api.get("/activity", token=tok["ada"]))["payments"][0]
    assert_rfc3339(payment["created_at"], "created_at")


def test_seeded_future_created_at_is_422_and_changes_nothing(api, reset):
    resp = api.post(
        "/_test/reset",
        body=make_fixture(
            payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, created_at=seconds_from_now(3600))]
        ),
    )
    expect(resp, 422, "validation_failed")
    tok = payload(login(api, "ada@example.com"))["token"]
    assert me(api, tok)["balance"] == 10000


def test_loading_seeded_payments_preserves_seeded_balance(api, reset):
    reset(
        make_fixture(
            payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, created_at="2026-09-01T00:00:00+00:00")]
        )
    )
    tok = relogin(api, "ada", "bob")
    assert me(api, tok["ada"])["balance"] == 10000
    assert me(api, tok["bob"])["balance"] == 2500


def test_api_created_payment_has_rfc3339_created_at(api, tokens):
    resp = api.post(
        "/payments", token=tokens["ada"], idem="ts-1", body={"to_handle": "bob", "amount": 10}
    )
    expect(resp, 201)
    assert_rfc3339(payload(resp)["created_at"], "created_at")
