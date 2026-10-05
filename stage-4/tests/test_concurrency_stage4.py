"""Stage-4 concurrency: refunds and correction batches under load."""

import json

from support import (
    Api,
    correction_item,
    create_payment,
    expect,
    login,
    make_fixture,
    me,
    payload,
    revisions,
    run_concurrently,
    seconds_from_now,
    statement,
    unique,
)


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def operator_fixture(**kwargs):
    return make_fixture(settlement_operator_ids=["u_ada"], **kwargs)


def pay(api, token, to_handle="bob", amount=500):
    resp = create_payment(api, token, to_handle, amount)
    expect(resp, 201)
    return payload(resp)["payment_id"]


def test_concurrent_refunds_never_exceed_corrected_amount(api, tokens):
    original = pay(api, tokens["ada"], "bob", 500)
    keys = [unique("ref") for _ in range(10)]

    def worker(index):
        return Api().post(
            f"/payments/{original}/refunds",
            token=tokens["bob"],
            idem=keys[index],
            body={"amount": 100},
        )

    results = run_concurrently(10, worker)
    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 5, statuses
    assert statuses.count(422) == 5, statuses
    for result in results:
        if result.status_code == 422:
            assert payload(result)["error"]["code"] == "refund_exceeds_payment"

    assert me(api, tokens["bob"])["balance"] == 2500
    assert me(api, tokens["ada"])["balance"] == 10000
    assert sum(me(api, tokens[h])["total"] for h in ("ada", "bob", "cy")) == 12500


def test_concurrent_identical_refund_replays_once(api, tokens):
    original = pay(api, tokens["ada"], "bob", 500)
    key = unique("ref")
    body = {"amount": 200}

    def worker(_):
        return Api().post(f"/payments/{original}/refunds", token=tokens["bob"], idem=key, body=body)

    results = run_concurrently(12, worker)
    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(200) == 11, statuses
    assert len({json.dumps(payload(r), sort_keys=True) for r in results}) == 1
    assert me(api, tokens["bob"])["balance"] == 2800


def test_concurrent_single_and_batch_correction_one_winner(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob")
    p1 = pay(api, tok["ada"], "bob", 500)
    effective_at = seconds_from_now(-60)

    def worker(index):
        if index == 0:
            return Api().post(
                f"/payments/{p1}/corrections",
                token=tok["ada"],
                idem=unique("corr"),
                body={"expected_revision": 1, "amount": 400, "effective_at": effective_at, "reason": "single"},
            )
        return Api().post(
            "/correction-batches",
            token=tok["ada"],
            idem=unique("batch"),
            body={"corrections": [correction_item(p1, amount=300, effective_at=effective_at)]},
        )

    results = run_concurrently(2, worker)
    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 409], [r.text for r in results]
    loser = next(r for r in results if r.status_code == 409)
    assert payload(loser)["error"]["code"] == "stale_revision"
    assert len(payload(revisions(api, tok["ada"], p1))["revisions"]) == 2


def test_concurrent_batches_same_payment_one_winner(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob", "cy")
    p1 = pay(api, tok["ada"], "bob", 500)
    effective_at = seconds_from_now(-60)
    keys = [unique("batch") for _ in range(6)]

    def worker(index):
        return Api().post(
            "/correction-batches",
            token=tok["ada"],
            idem=keys[index],
            body={"corrections": [correction_item(p1, amount=100 * (index + 2), effective_at=effective_at)]},
        )

    results = run_concurrently(6, worker)
    assert sum(1 for r in results if r.status_code == 201) == 1
    for result in results:
        if result.status_code != 201:
            assert result.status_code == 409, result.text
            assert payload(result)["error"]["code"] == "stale_revision"
    assert len(payload(revisions(api, tok["ada"], p1))["revisions"]) == 2

    views = [me(api, tok[h]) for h in ("ada", "bob", "cy")]
    assert sum(v["total"] for v in views) == 12500
    assert all(v["available"] >= 0 for v in views)


def test_snapshot_stable_during_concurrent_refunds(api, tokens):
    original = pay(api, tokens["ada"], "bob", 500)
    first = payload(statement(api, tokens["ada"]))
    token = first["snapshot"]
    keys = [unique("ref") for _ in range(10)]

    def worker(index):
        return Api().post(
            f"/payments/{original}/refunds",
            token=tokens["bob"],
            idem=keys[index],
            body={"amount": 100},
        )

    results = run_concurrently(10, worker)
    assert sum(1 for r in results if r.status_code == 201) == 5

    frozen = payload(statement(api, tokens["ada"], snapshot=token))
    assert frozen["entries"] == first["entries"]
    assert frozen["opening_balance"] == first["opening_balance"]

    fresh = payload(statement(api, tokens["ada"]))
    assert len(fresh["entries"]) > len(first["entries"])
