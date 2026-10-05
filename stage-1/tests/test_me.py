"""§8 GET /me plus seeded-balance semantics."""

from support import expect, login, make_fixture, payload, seeded_payment, signup, user


def test_me_returns_all_documented_fields(api, ada_token):
    resp = api.get("/me", token=ada_token)
    expect(resp, 200)
    body = payload(resp)
    assert set(body) == {"user_id", "display_name", "handle", "balance", "currency", "minor_units"}
    assert body == {
        "user_id": "u_ada",
        "display_name": "Ada",
        "handle": "ada",
        "balance": 10000,
        "currency": "EUR",
        "minor_units": 2,
    }


def test_me_reflects_seeded_balance_per_user(api, tokens):
    assert payload(api.get("/me", token=tokens["bob"]))["balance"] == 2500
    assert payload(api.get("/me", token=tokens["cy"]))["balance"] == 0


def test_me_does_not_replay_seeded_payments(api, reset):
    """`balance` in the fixture is already post-payment; reset must not debit again."""
    reset(
        make_fixture(
            users=[user("u_ada", "ada", 10000), user("u_bob", "bob", 2500)],
            payments=[seeded_payment("p_1", "u_ada", "u_bob", 500)],
        )
    )
    ada = payload(api.get("/me", token=payload(login(api, "ada@example.com"))["token"]))
    assert ada["balance"] == 10000


def test_me_currency_and_minor_units_follow_fixture(api, reset):
    reset(make_fixture(currency="JPY", minor_units=0, users=[user("u_ada", "ada", 1234)]))
    token = payload(login(api, "ada@example.com"))["token"]
    body = payload(api.get("/me", token=token))
    assert body["currency"] == "JPY"
    assert body["minor_units"] == 0
    assert body["balance"] == 1234


def test_me_balance_is_an_integer(api, ada_token):
    body = payload(api.get("/me", token=ada_token))
    assert isinstance(body["balance"], int) and not isinstance(body["balance"], bool)


def test_me_handle_matches_signup_derivation(api):
    resp = signup(api, "view.me@example.com")
    expect(resp, 201)
    token = payload(resp)["token"]
    body = payload(api.get("/me", token=token))
    assert body["handle"] == "view_me"
    assert body["balance"] == 0
