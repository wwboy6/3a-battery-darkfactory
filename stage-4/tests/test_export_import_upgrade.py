"""Stage-2 §10 + upgrade: export/import preserves holds and accepts stage-1 exports."""

from support import (
    authorize,
    capture,
    expect,
    login,
    make_fixture,
    me,
    payload,
    ttl_seconds,
    unique,
    void_authorization,
)


def export(api):
    resp = api.get("/_test/export")
    expect(resp, 200)
    return payload(resp)


def import_state(api, obj):
    return api.post("/_test/import", body=obj)


def relogin(api, handle="ada"):
    return payload(login(api, f"{handle}@example.com"))["token"]


def find_auth(api, token, aid):
    items = payload(api.get("/authorizations", token=token))["authorizations"]
    return next(a for a in items if a["authorization_id"] == aid)


def test_export_keeps_track_and_format_version(api):
    snap = export(api)
    assert snap["track"] == "pocketful"
    assert snap["format_version"] == 1
    assert "state" in snap


def test_roundtrip_preserves_open_hold_and_token(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    snap = export(api)
    void_authorization(api, tokens["ada"], aid)  # release the hold
    assert me(api, tokens["ada"])["held"] == 0

    expect(import_state(api, snap), 204)
    # The pre-import token still authenticates and the hold is back.
    assert me(api, tokens["ada"])["held"] == 2000
    assert find_auth(api, tokens["ada"], aid)["status"] == "open"


def test_roundtrip_preserves_capture_records(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 2000))["authorization_id"]
    p1 = payload(capture(api, tokens["bob"], aid, amount=700, final=False))
    snap = export(api)
    capture(api, tokens["bob"], aid, amount=1300)  # close it before the import

    expect(import_state(api, snap), 204)
    record = find_auth(api, tokens["ada"], aid)
    assert record["status"] == "open"
    assert record["captured_amount"] == 700
    assert record["remaining_amount"] == 1300
    assert record["payment_ids"] == [p1["payment_id"]]
    assert me(api, tokens["ada"])["total"] == 9300
    assert me(api, tokens["ada"])["held"] == 1300


def test_import_accepts_simulated_stage1_export(api, tokens):
    api.post("/payments", token=tokens["ada"], idem=unique("pay"), body={"to_handle": "bob", "amount": 100})
    snap = export(api)
    state = snap["state"]
    assert isinstance(state, dict), "export state must be a JSON object"

    removed = [
        key
        for key in ("authorizations", "authorization_ttl_seconds")
        if state.pop(key, None) is not None
    ]
    payments = state.get("payments")
    if isinstance(payments, list):
        for payment in payments:
            if isinstance(payment, dict):
                payment.pop("authorization_id", None)
    assert removed, "a stage-2 export should carry the authorization fields being stripped"

    expect(import_state(api, snap), 204)
    assert payload(api.get("/authorizations", token=tokens["ada"]))["authorizations"] == []
    assert me(api, tokens["ada"])["held"] == 0
    # Missing ttl falls back to the default 600.
    assert ttl_seconds(payload(authorize(api, tokens["ada"], "bob", 100))) == 600


def test_reset_clears_authorizations(api, reset, tokens):
    authorize(api, tokens["ada"], "bob", 2000)
    reset(make_fixture())
    tok = relogin(api)
    assert payload(api.get("/authorizations", token=tok))["authorizations"] == []
    assert me(api, tok)["held"] == 0
