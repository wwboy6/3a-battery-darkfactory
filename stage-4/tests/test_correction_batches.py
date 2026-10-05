"""Stage-4 POST /correction-batches: batches, settlement rules and atomicity."""

import pytest

from support import (
    assert_rfc3339,
    authorize,
    capture,
    correction,
    correction_batch,
    correction_item,
    create_payment,
    expect,
    login,
    make_fixture,
    me,
    payload,
    refund,
    revisions,
    seeded_payment,
    seconds_from_now,
    statement,
    unique,
    user,
)

T0 = "2026-09-09T12:00:00+00:00"
T1 = "2026-09-10T12:00:00+00:00"
T2 = "2026-09-11T12:00:00+00:00"


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def operator_fixture(**kwargs):
    return make_fixture(settlement_operator_ids=["u_ada"], **kwargs)


def pay(api, token, to_handle="bob", amount=500):
    resp = create_payment(api, token, to_handle, amount)
    expect(resp, 201)
    return payload(resp)["payment_id"]


def settle(api, token, transfers):
    resp = api.post("/settlements", token=token, idem=unique("stl"), body={"transfers": transfers})
    expect(resp, 201)
    return payload(resp)


def revs_of(api, token, pid):
    resp = revisions(api, token, pid)
    expect(resp, 200)
    return payload(resp)["revisions"]


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_batch_returns_batch_id_ordered_revisions_and_shared_recorded_at(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob", "cy")
    p1 = pay(api, tok["ada"], "bob", 500)
    p2 = pay(api, tok["ada"], "cy", 400)
    effective_at = seconds_from_now(-60)

    resp = correction_batch(
        api, tok["ada"],
        [correction_item(p1, amount=100, effective_at=effective_at),
         correction_item(p2, amount=50, effective_at=effective_at)],
    )
    expect(resp, 201)
    body = payload(resp)

    assert {"correction_batch_id", "recorded_at", "revisions"} <= set(body)
    assert_rfc3339(body["recorded_at"], "recorded_at")
    assert [r["payment_id"] for r in body["revisions"]] == [p1, p2]
    assert [r["amount"] for r in body["revisions"]] == [100, 50]
    for revision in body["revisions"]:
        assert revision["revision"] == 2
        assert revision["correction_batch_id"] == body["correction_batch_id"]
        assert revision["recorded_at"] == body["recorded_at"]

    assert body["correction_batch_id"] in [
        r["correction_batch_id"] for r in revs_of(api, tok["ada"], p1)
    ]
    assert sum(me(api, tok[h])["total"] for h in ("ada", "bob", "cy")) == 12500


def test_batch_revision_one_and_single_corrections_have_null_batch_id(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob")
    p1 = pay(api, tok["ada"], "bob", 500)
    p2 = pay(api, tok["ada"], "bob", 300)
    batch = payload(correction_batch(api, tok["ada"], [correction_item(p1, amount=100, effective_at=seconds_from_now(-60))]))
    single = payload(
        correction(
            api, tok["ada"], p2,
            expected_revision=1, amount=200, effective_at=seconds_from_now(-60), reason="one",
        )
    )

    first = revs_of(api, tok["ada"], p1)
    assert first[0].get("correction_batch_id") is None
    assert first[1]["correction_batch_id"] == batch["correction_batch_id"]

    second = revs_of(api, tok["ada"], p2)
    assert second[1].get("correction_batch_id") is None
    assert single["revision"] == 2


def test_batch_updates_statement_and_freezes_prior_snapshot(api, reset):
    reset(operator_fixture(payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, created_at=T1)]))
    tok = relogin(api, "ada", "bob")
    first = payload(statement(api, tok["ada"]))
    token = first["snapshot"]

    correction_batch(api, tok["ada"], [correction_item("p_1", amount=200, effective_at=seconds_from_now(-60))])

    fresh = payload(statement(api, tok["ada"]))
    entry = next(e for e in fresh["entries"] if e["payment"]["payment_id"] == "p_1")
    assert entry["payment"]["amount"] == 200
    assert entry["revision"] == 2

    frozen = payload(statement(api, tok["ada"], snapshot=token))
    assert frozen["entries"] == first["entries"]


# --------------------------------------------------------------------------- #
# Authorisation
# --------------------------------------------------------------------------- #


def test_batch_without_token_is_401(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob")
    p1 = pay(api, tok["ada"], "bob", 100)
    body = {"corrections": [correction_item(p1, amount=50, effective_at=seconds_from_now(-60))]}
    expect(api.post("/correction-batches", idem=unique("batch"), body=body), 401, "unauthenticated")


def test_batch_requires_operator(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob")
    p1 = pay(api, tok["ada"], "bob", 100)
    expect(
        correction_batch(api, tok["bob"], [correction_item(p1, amount=50, effective_at=seconds_from_now(-60))]),
        403,
        "forbidden",
    )


# --------------------------------------------------------------------------- #
# Validation and error precedence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        {"corrections": []},
        {"corrections": "nope"},
        {},
        {"corrections": [correction_item(f"p_{i}", amount=1, effective_at=T1) for i in range(33)]},
    ],
)
def test_batch_shape_and_size_are_422(api, reset, body):
    reset(operator_fixture())
    tok = relogin(api, "ada")
    expect(correction_batch(api, tok["ada"], None, body=body), 422, "validation_failed")


def test_batch_duplicate_payment_ids_are_422(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob")
    p1 = pay(api, tok["ada"], "bob", 500)
    items = [correction_item(p1, amount=100, effective_at=seconds_from_now(-60)) for _ in range(2)]
    expect(correction_batch(api, tok["ada"], items), 422, "validation_failed")


def test_batch_first_item_error_wins_unknown_then_invalid(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob")
    items = [
        correction_item("p_missing", amount=50, effective_at=seconds_from_now(-60)),
        correction_item(pay(api, tok["ada"], "bob", 100), amount=-1, effective_at=seconds_from_now(-60)),
    ]
    expect(correction_batch(api, tok["ada"], items), 404, "not_found")


def test_batch_first_item_error_wins_invalid_then_unknown(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob")
    p1 = pay(api, tok["ada"], "bob", 100)
    items = [
        correction_item(p1, amount=-1, effective_at=seconds_from_now(-60)),
        correction_item("p_missing", amount=50, effective_at=seconds_from_now(-60)),
    ]
    expect(correction_batch(api, tok["ada"], items), 422, "validation_failed")


def test_batch_stale_revision_is_409(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob")
    p1 = pay(api, tok["ada"], "bob", 500)
    expect(
        correction_batch(api, tok["ada"], [correction_item(p1, amount=100, effective_at=seconds_from_now(-60), expected_revision=2)]),
        409,
        "stale_revision",
    )
    assert len(revs_of(api, tok["ada"], p1)) == 1


def test_batch_future_effective_at_is_422(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob")
    p1 = pay(api, tok["ada"], "bob", 500)
    expect(
        correction_batch(api, tok["ada"], [correction_item(p1, amount=100, effective_at=seconds_from_now(3600))]),
        422,
        "validation_failed",
    )


# --------------------------------------------------------------------------- #
# Settlement membership rules
# --------------------------------------------------------------------------- #


def dual_member_settlement(api, tok):
    body = settle(
        api, tok["ada"],
        [
            {"from_handle": "ada", "to_handle": "bob", "amount": 500},
            {"from_handle": "bob", "to_handle": "cy", "amount": 200},
        ],
    )
    return [p["payment_id"] for p in body["payments"]]


def test_batch_settlement_member_requires_every_member(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob", "cy")
    members = dual_member_settlement(api, tok)
    effective_at = seconds_from_now(-60)

    expect(
        correction_batch(api, tok["ada"], [correction_item(members[0], amount=400, effective_at=effective_at)]),
        422,
        "incomplete_settlement",
    )
    expect(
        correction_batch(
            api, tok["ada"],
            [correction_item(members[0], amount=400, effective_at=effective_at),
             correction_item(members[1], amount=100, effective_at=effective_at)],
        ),
        201,
    )


def test_batch_settlement_members_share_one_instant(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob", "cy")
    members = dual_member_settlement(api, tok)

    expect(
        correction_batch(
            api, tok["ada"],
            [correction_item(members[0], amount=400, effective_at=seconds_from_now(-120)),
             correction_item(members[1], amount=100, effective_at=seconds_from_now(-60))],
        ),
        422,
        "validation_failed",
    )
    # Equivalent instants with different offset spellings are accepted.
    expect(
        correction_batch(
            api, tok["ada"],
            [correction_item(members[0], amount=400, effective_at="2026-09-20T12:00:00+00:00"),
             correction_item(members[1], amount=100, effective_at="2026-09-20T14:00:00+02:00")],
        ),
        201,
    )


def test_batch_cannot_touch_only_some_settlement_members_with_ordinary_items(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob", "cy")
    members = dual_member_settlement(api, tok)
    ordinary = pay(api, tok["ada"], "bob", 300)
    expect(
        correction_batch(
            api, tok["ada"],
            [correction_item(members[0], amount=400, effective_at=seconds_from_now(-60)),
             correction_item(ordinary, amount=200, effective_at=seconds_from_now(-60))],
        ),
        422,
        "incomplete_settlement",
    )


# --------------------------------------------------------------------------- #
# Affordability / atomicity
# --------------------------------------------------------------------------- #


def test_batch_combined_insufficient_funds_is_409_and_rolls_back(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob", "cy")
    p1 = pay(api, tok["ada"], "bob", 500)
    p2 = pay(api, tok["ada"], "cy", 500)
    before = {h: me(api, tok[h])["balance"] for h in ("ada", "bob", "cy")}

    expect(
        correction_batch(
            api, tok["ada"],
            [correction_item(p1, amount=6000, effective_at=seconds_from_now(-60)),
             correction_item(p2, amount=6000, effective_at=seconds_from_now(-60))],
        ),
        409,
        "insufficient_funds",
    )
    assert len(revs_of(api, tok["ada"], p1)) == 1
    assert {h: me(api, tok[h])["balance"] for h in ("ada", "bob", "cy")} == before


def test_batch_combined_historical_overdraft_is_409(api, reset):
    # ada opens at 800: seeded balance 1000 after -600 out (p_1/p_2 at T1) and +800 in
    # (p_3 at T2). At T1 the two outbound payments leave 200, but the inbound restores
    # the current balance. Each correction is therefore affordable now, yet together
    # they dip the T1 boundary below zero.
    reset(
        make_fixture(
            users=[user("u_ada", "ada", 1000), user("u_bob", "bob", 2500), user("u_cy", "cy", 2500)],
            payments=[
                seeded_payment("p_1", "u_ada", "u_bob", 300, created_at=T1),
                seeded_payment("p_2", "u_ada", "u_cy", 300, created_at=T1),
                seeded_payment("p_3", "u_bob", "u_ada", 800, created_at=T2),
            ],
            settlement_operator_ids=["u_ada"],
        )
    )
    tok = relogin(api, "ada", "bob", "cy")
    assert me(api, tok["ada"])["balance"] == 1000
    expect(
        correction_batch(
            api, tok["ada"],
            [correction_item("p_1", amount=500, effective_at=T0),
             correction_item("p_2", amount=500, effective_at=T0)],
        ),
        409,
        "historical_overdraft",
    )
    assert me(api, tok["ada"])["balance"] == 1000
    assert len(revs_of(api, tok["ada"], "p_1")) == 1


def test_batch_capture_is_immutable(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob")
    aid = payload(authorize(api, tok["ada"], "bob", 1000))["authorization_id"]
    captured = payload(capture(api, tok["bob"], aid, amount=500, final=False))
    expect(
        correction_batch(api, tok["ada"], [correction_item(captured["payment_id"], amount=300, effective_at=seconds_from_now(-60))]),
        422,
        "linked_payment_immutable",
    )


def test_batch_refund_floor_is_enforced(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob")
    p1 = pay(api, tok["ada"], "bob", 500)
    refund(api, tok["bob"], p1, amount=200)
    expect(
        correction_batch(api, tok["ada"], [correction_item(p1, amount=100, effective_at=seconds_from_now(-60))]),
        422,
        "refund_exceeds_payment",
    )


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_batch_replay_returns_original_body(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob", "cy")
    p1 = pay(api, tok["ada"], "bob", 500)
    p2 = pay(api, tok["ada"], "cy", 400)
    key = unique("batch")
    items = [
        correction_item(p1, amount=100, effective_at=seconds_from_now(-60)),
        correction_item(p2, amount=50, effective_at=seconds_from_now(-60)),
    ]
    first = payload(correction_batch(api, tok["ada"], items, key=key))
    replay = correction_batch(api, tok["ada"], items, key=key)
    expect(replay, 200)
    assert payload(replay) == first
    assert len(revs_of(api, tok["ada"], p1)) == 2


def test_batch_key_reuse_with_different_body_is_409(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob")
    p1 = pay(api, tok["ada"], "bob", 500)
    key = unique("batch")
    expect(correction_batch(api, tok["ada"], [correction_item(p1, amount=100, effective_at=seconds_from_now(-60))], key=key), 201)
    expect(
        correction_batch(api, tok["ada"], [correction_item(p1, amount=200, effective_at=seconds_from_now(-60))], key=key),
        409,
        "idempotency_key_reuse",
    )
