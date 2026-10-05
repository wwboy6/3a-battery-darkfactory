"""§4/§8 GET /activity: feed visibility contract and pagination."""

import time

from support import (
    expect,
    login,
    make_fixture,
    payload,
    seeded_payment,
    seeded_request,
    unique,
)


def pay(api, token, to_handle, amount, visibility="public"):
    return api.post(
        "/payments",
        token=token,
        idem=unique("pay"),
        body={"to_handle": to_handle, "amount": amount, "visibility": visibility},
    )


def activity_ids(api, token, **params):
    return [p["payment_id"] for p in payload(api.get("/activity", token=token, params=params or None))["payments"]]


def test_public_payment_visible_to_third_party(api, tokens):
    pid = payload(pay(api, tokens["ada"], "bob", 100, "public"))["payment_id"]
    assert pid in activity_ids(api, tokens["cy"])


def test_private_payment_hidden_from_third_party_but_visible_to_parties(api, tokens):
    pid = payload(pay(api, tokens["ada"], "bob", 100, "private"))["payment_id"]
    assert pid not in activity_ids(api, tokens["cy"])
    assert pid in activity_ids(api, tokens["ada"])
    assert pid in activity_ids(api, tokens["bob"])


def test_private_seeded_payment_visibility(api, reset):
    reset(
        make_fixture(
            payments=[seeded_payment("p_priv", "u_ada", "u_bob", 100, visibility="private")]
        )
    )
    tok = {h: payload(login(api, f"{h}@example.com"))["token"] for h in ("ada", "bob", "cy")}
    assert activity_ids(api, tok["cy"]) == []
    assert "p_priv" in activity_ids(api, tok["ada"])
    assert "p_priv" in activity_ids(api, tok["bob"])


def test_requests_never_appear_in_activity_feed(api, tokens):
    api.post(
        "/requests",
        token=tokens["bob"],
        idem=unique("req"),
        body={"payer_handle": "ada", "amount": 500},
    )
    assert activity_ids(api, tokens["ada"]) == []
    assert activity_ids(api, tokens["bob"]) == []


def test_split_itself_is_not_a_feed_item(api, tokens):
    api.post(
        "/splits",
        token=tokens["ada"],
        idem=unique("split"),
        body={"amount": 300, "participant_handles": ["ada", "bob"]},
    )
    assert activity_ids(api, tokens["ada"]) == []
    assert activity_ids(api, tokens["bob"]) == []


def test_activity_only_shows_visible_payments(api, tokens):
    public = payload(pay(api, tokens["ada"], "bob", 10, "public"))["payment_id"]
    private = payload(pay(api, tokens["bob"], "cy", 10, "private"))["payment_id"]
    ids = activity_ids(api, tokens["cy"])
    assert public in ids  # cy sees ada's public payment
    assert private in ids  # cy is the receiver of bob's private payment
    # ada cannot see bob's private payment
    assert private not in activity_ids(api, tokens["ada"])


def test_activity_pagination_and_has_more(api, tokens):
    for _ in range(3):
        pay(api, tokens["ada"], "bob", 10, "public")
    page1 = payload(api.get("/activity", token=tokens["cy"], params={"limit": 2, "offset": 0}))
    assert len(page1["payments"]) == 2 and page1["has_more"] is True
    page2 = payload(api.get("/activity", token=tokens["cy"], params={"limit": 2, "offset": 2}))
    assert len(page2["payments"]) == 1 and page2["has_more"] is False


def test_activity_is_newest_first_across_seconds(api, tokens):
    first = payload(pay(api, tokens["ada"], "bob", 10, "public"))["payment_id"]
    time.sleep(1.1)
    second = payload(pay(api, tokens["ada"], "bob", 10, "public"))["payment_id"]
    ids = activity_ids(api, tokens["cy"])
    assert ids.index(second) < ids.index(first)


def test_activity_empty_when_nothing_visible(api, tokens):
    assert payload(api.get("/activity", token=tokens["cy"]))["payments"] == []
