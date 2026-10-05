"""§1 invariants and §7 idempotency under concurrent load."""

import json

from support import (
    Api,
    expect,
    login,
    make_fixture,
    payload,
    run_concurrently,
    signup,
    unique,
    user,
)


def reauth(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def assert_no_5xx(results):
    assert all(r.status_code < 500 for r in results), [r.status_code for r in results]


# --------------------------------------------------------------------------- #
# Concurrent identical idempotent writes
# --------------------------------------------------------------------------- #


def test_concurrent_identical_payments_move_money_exactly_once(api, tokens):
    key = unique("idem")
    body = {"to_handle": "bob", "amount": 100}

    def worker(_):
        return Api().post("/payments", token=tokens["ada"], idem=key, body=body)

    results = run_concurrently(50, worker)
    assert_no_5xx(results)
    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(200) == 49, statuses
    bodies = {json.dumps(payload(r), sort_keys=True) for r in results}
    assert len(bodies) == 1
    assert payload(api.get("/me", token=tokens["ada"]))["balance"] == 9900
    assert payload(api.get("/me", token=tokens["bob"]))["balance"] == 2600


def test_concurrent_identical_request_creation_is_200_after_first(api, tokens):
    key = unique("idem")
    body = {"payer_handle": "ada", "amount": 33}

    def worker(_):
        return Api().post("/requests", token=tokens["bob"], idem=key, body=body)

    results = run_concurrently(30, worker)
    assert_no_5xx(results)
    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(200) == 29, statuses
    assert len(payload(api.get("/requests", token=tokens["bob"]))["requests"]) == 1


# --------------------------------------------------------------------------- #
# At-most-once money movement
# --------------------------------------------------------------------------- #


def test_concurrent_pay_of_same_request_moves_money_once(api, tokens):
    rid = payload(
        api.post("/requests", token=tokens["bob"], idem=unique("req"), body={"payer_handle": "ada", "amount": 500})
    )["request_id"]
    keys = [unique("pay") for _ in range(25)]

    def worker(index):
        return Api().post(f"/requests/{rid}/pay", token=tokens["ada"], idem=keys[index], body={})

    results = run_concurrently(25, worker)
    assert_no_5xx(results)
    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == 24, statuses
    assert all(r.status_code in (201, 409) for r in results)
    assert payload(api.get("/me", token=tokens["ada"]))["balance"] == 9500
    assert payload(api.get("/me", token=tokens["bob"]))["balance"] == 3000
    listed = payload(api.get("/requests", token=tokens["bob"]))["requests"][0]
    assert listed["status"] == "paid"


def test_concurrent_transfers_never_overspend_and_preserve_total(api, reset):
    reset(make_fixture(users=[user("u_ada", "ada", 1000), user("u_bob", "bob", 0), user("u_cy", "cy", 0)]))
    tok = reauth(api, "ada", "bob", "cy")
    keys = [unique("pay") for _ in range(50)]

    def worker(index):
        return Api().post(
            "/payments",
            token=tok["ada"],
            idem=keys[index],
            body={"to_handle": "bob", "amount": 100},
        )

    results = run_concurrently(50, worker)
    assert_no_5xx(results)
    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 10, statuses
    assert statuses.count(409) == 40, statuses
    balances = [payload(api.get("/me", token=tok[h]))["balance"] for h in ("ada", "bob", "cy")]
    assert balances == [0, 1000, 0]
    assert sum(balances) == 1000


def test_concurrent_mixed_transfers_preserve_total(api, reset):
    reset(make_fixture(users=[user("u_ada", "ada", 1000), user("u_bob", "bob", 0), user("u_cy", "cy", 0)]))
    tok = reauth(api, "ada", "bob", "cy")
    keys = [unique("pay") for _ in range(40)]

    def worker(index):
        target = "bob" if index % 2 == 0 else "cy"
        return Api().post(
            "/payments",
            token=tok["ada"],
            idem=keys[index],
            body={"to_handle": target, "amount": 100},
        )

    results = run_concurrently(40, worker)
    assert_no_5xx(results)
    assert sum(1 for r in results if r.status_code == 201) == 10
    balances = {h: payload(api.get("/me", token=tok[h]))["balance"] for h in ("ada", "bob", "cy")}
    assert balances["ada"] == 0
    assert balances["bob"] + balances["cy"] == 1000
    assert sum(balances.values()) == 1000


# --------------------------------------------------------------------------- #
# Race on unique constraints
# --------------------------------------------------------------------------- #


def test_concurrent_signup_same_derived_handle_creates_exactly_one_account(api):
    emails = [f"RACE@x{i}.example.com" for i in range(8)]

    def worker(index):
        return Api().post(
            "/auth/signup",
            body={"email": emails[index], "password": "correct horse", "display_name": "R"},
        )

    results = run_concurrently(8, worker)
    assert_no_5xx(results)
    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == 7, statuses
    for r in results:
        if r.status_code == 409:
            assert payload(r)["error"]["code"] == "handle_taken"
