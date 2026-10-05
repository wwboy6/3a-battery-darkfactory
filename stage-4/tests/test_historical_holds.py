"""Stage-3 historical holds: /me?as_of&known_at and closed_at."""

from support import (
    assert_rfc3339,
    authorize,
    capture,
    expect,
    get_me,
    login,
    make_fixture,
    payload,
    seconds_from_now,
    seeded_authorization,
    statement,
    void_authorization,
)


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def open_hold_fixture(*, created_at=-7200, expires_at=7200, amount=2000):
    return make_fixture(
        authorizations=[
            seeded_authorization(
                "a_1", "u_ada", "u_bob", amount,
                created_at=seconds_from_now(created_at),
                expires_at=seconds_from_now(expires_at),
            )
        ]
    )


def find_auth(api, token, aid="a_1"):
    items = payload(api.get("/authorizations", token=token))["authorizations"]
    return next(a for a in items if a["authorization_id"] == aid)


def assert_view_consistent(body):
    assert body["balance"] == body["total"]
    assert body["available"] == body["total"] - body["held"]
    assert body["held"] >= 0


def test_closed_at_null_while_open_then_set(api, reset):
    reset(open_hold_fixture())
    tok = relogin(api, "ada")["ada"]
    assert find_auth(api, tok)["closed_at"] is None
    void_authorization(api, tok, "a_1")
    record = find_auth(api, tok)
    assert record["status"] == "voided"
    assert record["closed_at"] is not None
    assert_rfc3339(record["closed_at"], "closed_at")


def test_hold_present_between_creation_and_now(api, reset):
    reset(open_hold_fixture())
    tok = relogin(api, "ada")["ada"]
    body = payload(get_me(api, tok, as_of=seconds_from_now(-3600)))
    assert body["held"] == 2000
    assert body["available"] == 8000
    assert_view_consistent(body)


def test_hold_absent_before_creation(api, reset):
    reset(open_hold_fixture())
    tok = relogin(api, "ada")["ada"]
    body = payload(get_me(api, tok, as_of=seconds_from_now(-10000)))
    assert body["held"] == 0
    assert body["available"] == 10000


def test_seeded_hold_without_created_at_starts_at_reset(api, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization("a_1", "u_ada", "u_bob", 2000, expires_at=seconds_from_now(7200))
            ]
        )
    )
    tok = relogin(api, "ada")["ada"]
    before = payload(get_me(api, tok, as_of=seconds_from_now(-120)))
    assert before["held"] == 0
    after = payload(get_me(api, tok, as_of=seconds_from_now(3600)))
    assert after["held"] == 2000


def test_nonfinal_capture_reduces_held_at_capture_time(api, reset):
    reset(open_hold_fixture())
    tok = relogin(api, "ada", "bob")
    capture(api, tok["bob"], "a_1", amount=1500, final=False)

    before_capture = payload(get_me(api, tok["ada"], as_of=seconds_from_now(-3600)))
    assert before_capture["held"] == 2000

    after_capture = payload(get_me(api, tok["ada"], as_of=seconds_from_now(3600)))
    assert after_capture["held"] == 500
    assert after_capture["total"] == 8500
    assert after_capture["available"] == 8000
    assert_view_consistent(after_capture)


def test_final_capture_releases_remainder(api, reset):
    reset(open_hold_fixture())
    tok = relogin(api, "ada", "bob")
    capture(api, tok["bob"], "a_1", amount=1500)
    after = payload(get_me(api, tok["ada"], as_of=seconds_from_now(3600)))
    assert after["held"] == 0
    assert after["total"] == 8500
    assert after["available"] == 8500


def test_void_releases_remainder_at_event_time(api, reset):
    reset(open_hold_fixture())
    tok = relogin(api, "ada")["ada"]
    void_authorization(api, tok, "a_1")
    before = payload(get_me(api, tok, as_of=seconds_from_now(-3600)))
    assert before["held"] == 2000
    after = payload(get_me(api, tok, as_of=seconds_from_now(3600)))
    assert after["held"] == 0 and after["available"] == 10000


def test_open_hold_expires_at_deadline_for_future_queries(api, reset):
    reset(open_hold_fixture(created_at=-7200, expires_at=3600))
    tok = relogin(api, "ada")["ada"]
    now = payload(get_me(api, tok, as_of=seconds_from_now(60)))
    assert now["held"] == 2000
    beyond = payload(get_me(api, tok, as_of=seconds_from_now(7200)))
    assert beyond["held"] == 0 and beyond["available"] == 10000


def test_capture_appears_once_and_holds_are_not_statement_entries(api, reset):
    reset(open_hold_fixture())
    tok = relogin(api, "ada", "bob")
    payment = payload(capture(api, tok["bob"], "a_1", amount=1000, final=False))

    body = payload(statement(api, tok["ada"]))
    linked = [e for e in body["entries"] if e["payment"].get("authorization_id") == "a_1"]
    assert len(linked) == 1
    assert linked[0]["payment"]["payment_id"] == payment["payment_id"]

    void_authorization(api, tok["ada"], "a_1")
    after = payload(statement(api, tok["ada"]))
    assert [e["payment"]["payment_id"] for e in after["entries"]] == [payment["payment_id"]]
