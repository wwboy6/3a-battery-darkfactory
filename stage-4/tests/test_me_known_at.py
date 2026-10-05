"""Stage-3 GET /me?known_at: revision selection by recorded time."""

from support import (
    expect,
    get_me,
    login,
    make_fixture,
    payload,
    seeded_payment,
    seconds_from_now,
    unique,
)

K1 = "2026-09-10T12:00:00+00:00"


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def history_fixture():
    return make_fixture(payments=[seeded_payment("p_0", "u_ada", "u_bob", 500, created_at=K1)])


def test_known_at_before_any_record_is_opening(api, reset):
    reset(history_fixture())
    tok = relogin(api, "ada")["ada"]
    body = payload(get_me(api, tok, known_at="2026-09-10T11:00:00+00:00"))
    assert body["balance"] == 10500


def test_known_at_selects_original_recorded_revision(api, reset, tokens):
    reset(history_fixture())
    tok = relogin(api, "ada")["ada"]
    # A new API payment is recorded now, after K1.
    expect(
        api.post("/payments", token=tok, idem=unique("pay"), body={"to_handle": "bob", "amount": 300}),
        201,
    )
    at_k1 = payload(get_me(api, tok, known_at=K1))
    assert at_k1["balance"] == 10000  # only p_0 known at K1

    now_later = payload(get_me(api, tok, known_at=seconds_from_now(3600)))
    assert now_later["balance"] == 9700  # both payments known


def test_known_at_echoed_exactly(api, tokens):
    value = "2026-09-10T18:00:00+02:00"
    body = payload(get_me(api, tokens["ada"], known_at=value))
    assert body["known_at"] == value


def test_known_at_and_as_of_combine(api, reset):
    reset(history_fixture())
    tok = relogin(api, "ada")["ada"]
    expect(
        api.post("/payments", token=tok, idem=unique("pay"), body={"to_handle": "bob", "amount": 300}),
        201,
    )
    # Everything known, but only movements effective at or before K1 count.
    body = payload(get_me(api, tok, known_at=seconds_from_now(3600), as_of=K1))
    assert body["balance"] == 10000


def test_known_at_future_includes_all(api, reset):
    reset(history_fixture())
    tok = relogin(api, "ada")["ada"]
    body = payload(get_me(api, tok, known_at=seconds_from_now(86400)))
    assert body["balance"] == 10000
