"""Concurrency with holds: totals are conserved and ``available`` never goes negative.

Concurrent requests must behave like some serial order. These tests race several
clients against a wallet that has an open hold and assert the stage-2 invariants
at the end.
"""

from __future__ import annotations

import json

from support import (
    Api,
    make_fixture,
    me,
    payload,
    run_concurrently,
    seeded_authorization,
    unique,
)


def assert_no_5xx(results):
    assert all(r.status_code < 500 for r in results), [r.status_code for r in results]


def total_sum(api, tokens):
    return sum(me(api, tokens[h])["total"] for h in ("ada", "bob", "cy"))


def test_concurrent_payments_respect_available_not_total(api, tokens, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    assert me(api, tokens["ada"])["available"] == 8000

    def worker(_):
        return Api().post(
            "/payments",
            token=tokens["ada"],
            idem=unique("conc-pay"),
            body={"to_handle": "bob", "amount": 500},
        )

    results = run_concurrently(20, worker)
    assert_no_5xx(results)
    created = [r for r in results if r.status_code == 201]
    refused = [r for r in results if r.status_code == 409]
    assert len(created) + len(refused) == 20
    assert all(payload(r)["error"]["code"] == "insufficient_funds" for r in refused)

    # 8000 available funds only 16 payments of 500, never the full 10000 total.
    assert len(created) == 16, [r.status_code for r in results]
    account = me(api, tokens["ada"])
    assert account["held"] == 2000
    assert account["available"] == 8000 - 500 * len(created) == 0
    assert total_sum(api, tokens) == 12500


def test_concurrent_identical_capture_moves_money_once(api, tokens, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))
    key = unique("conc-cap")
    body = {}

    def worker(_):
        return Api().post(
            "/authorizations/a_1/capture",
            token=tokens["bob"],
            idem=key,
            body=body,
        )

    results = run_concurrently(20, worker)
    assert_no_5xx(results)
    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(200) == 19, statuses
    bodies = {json.dumps(payload(r), sort_keys=True) for r in results}
    assert len(bodies) == 1

    assert me(api, tokens["bob"])["total"] == 4500
    assert me(api, tokens["ada"])["total"] == 8000
    assert me(api, tokens["ada"])["available"] == 8000
    assert total_sum(api, tokens) == 12500


def test_concurrent_partial_captures_never_exceed_authorized_amount(api, tokens, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))

    def worker(_):
        return Api().post(
            "/authorizations/a_1/capture",
            token=tokens["bob"],
            idem=unique("conc-part"),
            body={"amount": 500, "final": False},
        )

    results = run_concurrently(8, worker)
    assert_no_5xx(results)
    created = [r for r in results if r.status_code == 201]
    # 500 * 4 == 2000; the hold closes on the fourth capture and the rest are refused.
    assert len(created) == 4, [r.status_code for r in results]
    assert sum(payload(r)["amount"] for r in created) == 2000
    assert me(api, tokens["bob"])["total"] == 4500
    account = me(api, tokens["ada"])
    assert account["held"] == 0 and account["available"] == 8000
    assert total_sum(api, tokens) == 12500


def test_concurrent_holds_cannot_overdraw_wallet(api, tokens, reset):
    reset(make_fixture())

    def worker(_):
        return Api().post(
            "/authorizations",
            token=tokens["ada"],
            idem=unique("conc-auth"),
            body={"to_handle": "bob", "amount": 1000},
        )

    results = run_concurrently(24, worker)
    assert_no_5xx(results)
    created = [r for r in results if r.status_code == 201]
    refused = [r for r in results if r.status_code == 409]
    assert len(created) + len(refused) == 24
    assert all(payload(r)["error"]["code"] == "insufficient_funds" for r in refused)
    assert len(created) == 10, [r.status_code for r in results]

    account = me(api, tokens["ada"])
    assert account["held"] == 1000 * len(created) == 10000
    assert account["available"] == 0
    assert account["available"] >= 0
    assert total_sum(api, tokens) == 12500


def test_payments_and_capture_race_keep_invariants(api, tokens, reset):
    reset(make_fixture(authorizations=[seeded_authorization("a_1", "u_ada", "u_bob", 2000)]))

    def payer(_):
        return Api().post(
            "/payments",
            token=tokens["ada"],
            idem=unique("race-pay"),
            body={"to_handle": "bob", "amount": 300},
        )

    def capturer(_):
        return Api().post(
            "/authorizations/a_1/capture",
            token=tokens["bob"],
            idem=unique("race-cap"),
            body={"amount": 2000},
        )

    results = run_concurrently(11, lambda i: capturer(i) if i == 0 else payer(i))
    assert_no_5xx(results)
    assert total_sum(api, tokens) == 12500
    for handle in ("ada", "bob", "cy"):
        account = me(api, tokens[handle])
        assert account["available"] >= 0
        assert account["balance"] == account["total"]
