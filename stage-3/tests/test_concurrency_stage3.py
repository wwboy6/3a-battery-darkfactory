"""Stage-3 concurrency: corrections, snapshots and total invariants under load."""

import json

from support import (
    Api,
    correction,
    expect,
    login,
    make_fixture,
    me,
    payload,
    run_concurrently,
    seconds_from_now,
    statement,
    unique,
    user,
)


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def assert_no_5xx(results):
    assert all(r.status_code < 500 for r in results), [r.status_code for r in results]


def make_payment(api, token, amount=500, to_handle="bob"):
    resp = api.post("/payments", token=token, idem=unique("pay"), body={"to_handle": to_handle, "amount": amount})
    expect(resp, 201)
    return payload(resp)["payment_id"]


def correct(pid, token, key, *, expected_revision=1, amount, effective_at, reason="x"):
    return Api().post(
        f"/payments/{pid}/corrections",
        token=token,
        idem=key,
        body={
            "expected_revision": expected_revision,
            "amount": amount,
            "effective_at": effective_at,
            "reason": reason,
        },
    )


# --------------------------------------------------------------------------- #
# Stale revisions under load
# --------------------------------------------------------------------------- #


def test_concurrent_corrections_same_expected_revision_only_one_wins(api, tokens):
    pid = make_payment(api, tokens["ada"], 500)
    effective_at = seconds_from_now(-60)
    keys = [unique("corr") for _ in range(12)]

    def worker(index):
        return correct(pid, tokens["ada"], keys[index], amount=400, effective_at=effective_at, reason=f"r{index}")

    results = run_concurrently(12, worker)
    assert_no_5xx(results)
    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == 11, statuses
    for result in results:
        if result.status_code == 409:
            assert payload(result)["error"]["code"] == "stale_revision"

    assert len(payload(api.get(f"/payments/{pid}/revisions", token=tokens["ada"]))["revisions"]) == 2
    assert me(api, tokens["ada"])["balance"] == 9600
    assert me(api, tokens["bob"])["balance"] == 2900


def test_concurrent_identical_correction_replays_once(api, tokens):
    pid = make_payment(api, tokens["ada"], 500)
    effective_at = seconds_from_now(-60)
    key = unique("corr")
    body = {"expected_revision": 1, "amount": 300, "effective_at": effective_at, "reason": "fix"}

    def worker(_):
        return Api().post(f"/payments/{pid}/corrections", token=tokens["ada"], idem=key, body=body)

    results = run_concurrently(20, worker)
    assert_no_5xx(results)
    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(200) == 19, statuses
    assert len({json.dumps(payload(r), sort_keys=True) for r in results}) == 1
    assert len(payload(api.get(f"/payments/{pid}/revisions", token=tokens["ada"]))["revisions"]) == 2


# --------------------------------------------------------------------------- #
# Invariants under concurrent writes
# --------------------------------------------------------------------------- #


def test_concurrent_corrections_preserve_total_and_available(api, tokens):
    pid = make_payment(api, tokens["ada"], 500)
    effective_at = seconds_from_now(-60)
    keys = [unique("corr") for _ in range(16)]

    def worker(index):
        return correct(pid, tokens["ada"], keys[index], amount=100 * (index + 1), effective_at=effective_at)

    results = run_concurrently(16, worker)
    assert_no_5xx(results)
    assert sum(1 for r in results if r.status_code == 201) == 1

    totals = [me(api, tokens[h])["total"] for h in ("ada", "bob", "cy")]
    assert sum(totals) == 12500
    for handle in ("ada", "bob", "cy"):
        view = me(api, tokens[handle])
        assert view["available"] >= 0 and view["held"] >= 0
        assert view["available"] == view["total"] - view["held"]


def test_concurrent_corrections_never_overdraw(api, reset):
    reset(make_fixture(users=[user("u_ada", "ada", 1000), user("u_bob", "bob", 0)]))
    tok = relogin(api, "ada", "bob")
    pid = make_payment(api, tok["ada"], 500)
    effective_at = seconds_from_now(-60)
    keys = [unique("corr") for _ in range(10)]

    def worker(index):
        return correct(pid, tok["ada"], keys[index], amount=900, effective_at=effective_at)

    results = run_concurrently(10, worker)
    assert_no_5xx(results)
    assert sum(1 for r in results if r.status_code == 201) == 1

    ada = me(api, tok["ada"])["balance"]
    bob = me(api, tok["bob"])["balance"]
    assert ada == 100 and bob == 900
    assert ada + bob == 1000


def test_concurrent_payments_and_corrections_preserve_total(api, tokens):
    pid = make_payment(api, tokens["ada"], 500)
    effective_at = seconds_from_now(-60)
    pay_keys = [unique("pay") for _ in range(10)]
    corr_keys = [unique("corr") for _ in range(10)]

    def worker(index):
        if index % 2 == 0:
            return Api().post(
                "/payments", token=tokens["ada"], idem=pay_keys[index // 2],
                body={"to_handle": "cy", "amount": 1},
            )
        return correct(pid, tokens["ada"], corr_keys[index // 2], amount=400, effective_at=effective_at)

    results = run_concurrently(20, worker)
    assert_no_5xx(results)

    views = {h: me(api, tokens[h]) for h in ("ada", "bob", "cy")}
    assert sum(v["total"] for v in views.values()) == 12500
    assert all(v["available"] >= 0 for v in views.values())
    assert sum(1 for r in results if r.status_code == 201) >= 10


# --------------------------------------------------------------------------- #
# Snapshots are stable while corrections run
# --------------------------------------------------------------------------- #


def test_snapshot_stable_during_concurrent_corrections(api, tokens):
    pid = make_payment(api, tokens["ada"], 500)
    first = payload(statement(api, tokens["ada"]))
    token = first["snapshot"]
    effective_at = seconds_from_now(-60)
    keys = [unique("corr") for _ in range(12)]

    def worker(index):
        return correct(pid, tokens["ada"], keys[index], amount=100, effective_at=effective_at)

    results = run_concurrently(12, worker)
    assert_no_5xx(results)
    assert sum(1 for r in results if r.status_code == 201) == 1

    paged = payload(statement(api, tokens["ada"], snapshot=token))
    assert paged["entries"] == first["entries"]
    assert paged["opening_balance"] == first["opening_balance"]

    fresh = payload(statement(api, tokens["ada"]))
    entry = next(e for e in fresh["entries"] if e["payment"]["payment_id"] == pid)
    assert entry["payment"]["amount"] == 100
    assert entry["revision"] == 2
