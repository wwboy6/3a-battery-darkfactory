"""§10 export/import: round-trip fidelity, replacement and validation."""

import pytest

from support import expect, login, payload, signup, unique


def export(api):
    resp = api.get("/_test/export")
    expect(resp, 200)
    return payload(resp)


def import_state(api, obj):
    return api.post("/_test/import", body=obj)


def make_payment(api, token, key=None, amount=100):
    return api.post(
        "/payments",
        token=token,
        idem=key or unique("idem"),
        body={"to_handle": "bob", "amount": amount},
    )


# --------------------------------------------------------------------------- #
# Export shape
# --------------------------------------------------------------------------- #


def test_export_shape_and_no_auth_required(api):
    body = export(api)
    assert body["track"] == "pocketful"
    assert body["format_version"] == 1
    assert "state" in body


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #


def test_export_import_export_is_lossless(api, tokens):
    make_payment(api, tokens["ada"], amount=250)
    first = export(api)
    expect(import_state(api, first), 204)
    second = export(api)
    assert second["state"] == first["state"]


def test_import_preserves_tokens_and_balances(api, tokens):
    make_payment(api, tokens["ada"], amount=250)
    snapshot = export(api)
    expect(import_state(api, snapshot), 204)
    # The pre-import token still authenticates.
    me = api.get("/me", token=tokens["ada"])
    expect(me, 200)
    assert payload(me)["balance"] == 9750


def test_import_preserves_password_login(api, tokens):
    signup(api, "persist@example.com", display_name="Persist")
    snapshot = export(api)
    expect(import_state(api, snapshot), 204)
    resp = login(api, "persist@example.com")
    expect(resp, 200)
    assert payload(resp)["display_name"] == "Persist"


def test_import_preserves_payments_and_requests(api, tokens):
    make_payment(api, tokens["ada"], amount=100)
    api.post("/requests", token=tokens["bob"], idem=unique("req"), body={"payer_handle": "ada", "amount": 42})
    snapshot = export(api)
    expect(import_state(api, snapshot), 204)
    feed = payload(api.get("/activity", token=tokens["ada"]))["payments"]
    assert len(feed) == 1 and feed[0]["amount"] == 100
    requests = payload(api.get("/requests", token=tokens["bob"]))["requests"]
    assert len(requests) == 1 and requests[0]["amount"] == 42


def test_import_preserves_completed_idempotent_responses(api, tokens):
    key = unique("idem")
    first = make_payment(api, tokens["ada"], key=key, amount=100)
    expect(first, 201)
    snapshot = export(api)
    expect(import_state(api, snapshot), 204)
    replay = make_payment(api, tokens["ada"], key=key, amount=100)
    expect(replay, 200)
    assert payload(replay) == payload(first)
    assert payload(api.get("/me", token=tokens["ada"]))["balance"] == 9900


def test_failed_request_key_remains_reusable_after_import(api, tokens):
    key = unique("idem")
    expect(make_payment(api, tokens["ada"], key=key, amount=0), 422, "validation_failed")
    snapshot = export(api)
    expect(import_state(api, snapshot), 204)
    expect(make_payment(api, tokens["ada"], key=key, amount=10), 201)


def test_import_preserves_settlement_operator_permission(api, reset):
    from support import make_fixture

    reset(make_fixture(settlement_operator_ids=["u_ada"]))
    tok = payload(login(api, "ada@example.com"))["token"]
    snapshot = export(api)

    reset(make_fixture())  # operator permission gone
    expect(import_state(api, snapshot), 204)
    resp = api.post(
        "/settlements",
        token=tok,
        idem=unique("idem"),
        body={"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 1}]},
    )
    expect(resp, 201)


# --------------------------------------------------------------------------- #
# Replacement semantics
# --------------------------------------------------------------------------- #


def test_import_replaces_rather_than_merges(api, tokens):
    snapshot = export(api)
    make_payment(api, tokens["ada"], amount=100)
    signup(api, "destination-only@example.com")
    expect(import_state(api, snapshot), 204)
    assert payload(api.get("/activity", token=tokens["ada"]))["payments"] == []
    expect(login(api, "destination-only@example.com"), 401, "unauthenticated")


def test_repeated_import_does_not_duplicate(api, tokens):
    make_payment(api, tokens["ada"], amount=100)
    snapshot = export(api)
    expect(import_state(api, snapshot), 204)
    expect(import_state(api, snapshot), 204)
    assert len(payload(api.get("/activity", token=tokens["ada"]))["payments"]) == 1


def test_export_is_an_immutable_snapshot(api, tokens):
    snapshot = export(api)
    make_payment(api, tokens["ada"], amount=100)
    expect(import_state(api, snapshot), 204)
    assert payload(api.get("/activity", token=tokens["ada"]))["payments"] == []


def test_reset_clears_imported_state(api, reset, tokens):
    signup(api, "imported@example.com")
    snapshot = export(api)
    reset(make_fixture())
    expect(login(api, "imported@example.com"), 401, "unauthenticated")
    expect(import_state(api, snapshot), 204)
    expect(login(api, "imported@example.com"), 200)
    reset(make_fixture())
    expect(login(api, "imported@example.com"), 401, "unauthenticated")


# --------------------------------------------------------------------------- #
# Invalid imports
# --------------------------------------------------------------------------- #


def test_import_unparseable_body_is_400(api):
    expect(api.post("/_test/import", raw_body="{not json"), 400, "malformed_request")


def test_import_missing_state_is_422(api):
    expect(import_state(api, {"track": "pocketful", "format_version": 1}), 422, "validation_failed")


def test_import_wrong_track_is_422(api):
    expect(import_state(api, {"track": "other", "format_version": 1, "state": {}}), 422, "validation_failed")


def test_import_wrong_format_version_is_422(api):
    expect(import_state(api, {"track": "pocketful", "format_version": 2, "state": {}}), 422, "validation_failed")


def test_import_invalid_state_is_422(api):
    expect(import_state(api, {"track": "pocketful", "format_version": 1, "state": 123}), 422, "validation_failed")


def test_invalid_import_leaves_destination_untouched(api, tokens):
    make_payment(api, tokens["ada"], amount=100)
    expect(import_state(api, {"track": "other", "format_version": 1, "state": {}}), 422, "validation_failed")
    assert len(payload(api.get("/activity", token=tokens["ada"]))["payments"]) == 1
    assert payload(api.get("/me", token=tokens["ada"]))["balance"] == 9900
