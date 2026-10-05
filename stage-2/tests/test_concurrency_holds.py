"""Stage-2 §1: holds, captures and the total invariant under concurrency."""

import json

from support import (
    Api,
    authorize,
    capture,
    expect,
    login,
    me,
    payload,
    run_concurrently,
    unique,
)


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def assert_no_5xx(results):
    assert all(r.status_code < 500 for r in results), [r.status_code for r in results]


def test_concurrent_identical_capture_moves_money_once(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    key = unique("cap")

    def worker(_):
        return Api().post(f"/authorizations/{aid}/capture", token=tokens["bob"], idem=key, body={})

    results = run_concurrently(25, worker)
    assert_no_5xx(results)
    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(200) == 24, statuses
    assert len({json.dumps(payload(r), sort_keys=True) for r in results}) == 1
    assert me(api, tokens["ada"])["total"] == 8000


def test_concurrent_distinct_captures_never_exceed_remainder(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 1000))["authorization_id"]
    keys = [unique("cap") for _ in range(10)]

    def worker(index):
        return Api().post(
            f"/authorizations/{aid}/capture",
            token=tokens["bob"],
            idem=keys[index],
            body={"amount": 400, "final": False},
        )

    results = run_concurrently(10, worker)
    assert_no_5xx(results)
    successes = [r for r in results if r.status_code == 201]
    captured = sum(payload(r)["amount"] for r in successes)
    assert captured == 800, [r.status_code for r in results]
    for r in results:
        if r.status_code != 201:
            assert r.status_code == 422, r.text
            assert payload(r)["error"]["code"] == "capture_exceeds_authorization"

    ada = me(api, tokens["ada"])
    assert ada["total"] == 10000 - captured
    assert ada["held"] == 1000 - captured
    assert ada["available"] == 9000


def test_concurrent_payments_respect_available_with_hold(api, tokens):
    authorize(api, tokens["ada"], "bob", 6000)  # available 4000
    keys = [unique("pay") for _ in range(20)]

    def worker(index):
        return Api().post(
            "/payments", token=tokens["ada"], idem=keys[index],
            body={"to_handle": "cy", "amount": 400},
        )

    results = run_concurrently(20, worker)
    assert_no_5xx(results)
    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 10, statuses
    assert statuses.count(409) == 10, statuses

    ada = me(api, tokens["ada"])
    assert ada["total"] == 6000 and ada["held"] == 6000 and ada["available"] == 0
    assert me(api, tokens["cy"])["total"] == 4000


def test_total_conserved_with_holds_and_captures(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    capture(api, tokens["bob"], aid, amount=1500, final=False)
    total = sum(me(api, tokens[h])["total"] for h in ("ada", "bob", "cy"))
    assert total == 12500
    assert me(api, tokens["ada"])["held"] == 500
