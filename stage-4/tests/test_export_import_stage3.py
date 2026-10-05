"""Stage-3 export/import upgrade.

Round-trips the temporal ledger and proves the service still accepts exports
produced by the stage-1 and stage-2 services. The state blob is
implementation-defined, so older exports are simulated by projecting a live
stage-3 export onto the documented stage-1/stage-2 field sets: any stage-3-only
collection or field is dropped, forcing the importer to derive revision 1,
opening balances and hold state.
"""

from support import (
    authorize,
    capture,
    correction,
    expect,
    get_me,
    login,
    make_fixture,
    me,
    payload,
    revisions,
    seconds_from_now,
    seeded_authorization,
    seeded_payment,
    statement,
    unique,
    void_authorization,
)

T1 = "2026-09-10T12:00:00+00:00"
BEFORE = "2026-09-10T11:00:00+00:00"


def export(api):
    resp = api.get("/_test/export")
    expect(resp, 200)
    return payload(resp)


def import_state(api, obj):
    return api.post("/_test/import", body=obj)


def relogin(api, *handles):
    return {h: payload(login(api, f"{h}@example.com"))["token"] for h in handles}


def find_auth(api, token, aid):
    items = payload(api.get("/authorizations", token=token))["authorizations"]
    return next(a for a in items if a["authorization_id"] == aid)


# --------------------------------------------------------------------------- #
# Projecting a live export onto the older schemas
# --------------------------------------------------------------------------- #

STAGE1_STATE_KEYS = {
    "currency", "minor_units", "users", "payments", "payment_order",
    "requests", "request_order", "settlements", "operators", "idempotency",
}
STAGE2_STATE_KEYS = STAGE1_STATE_KEYS | {
    "authorization_ttl_seconds", "authorizations", "authorization_order",
}
STAGE1_USER_KEYS = {"id", "email", "password_hash", "display_name", "handle", "balance", "tokens"}
STAGE1_PAYMENT_KEYS = {
    "id", "from_user_id", "to_user_id", "amount", "currency", "note",
    "visibility", "request_id", "settlement_id", "created_at",
}
STAGE2_PAYMENT_KEYS = STAGE1_PAYMENT_KEYS | {"authorization_id"}
STAGE1_REQUEST_KEYS = {
    "id", "requester_id", "payer_id", "amount", "currency", "note",
    "status", "payment_id", "created_at",
}
STAGE1_SETTLEMENT_KEYS = {"id", "committed_at", "payment_ids"}
STAGE1_IDEM_KEYS = {"user_id", "key", "method", "path", "body", "response", "status"}
STAGE2_AUTH_KEYS = {
    "id", "from_user_id", "to_user_id", "amount", "captured_amount", "currency",
    "note", "visibility", "status", "expires_at", "payment_id", "payment_ids",
    "created_at",
}


def _pick(record, keys):
    if not isinstance(record, dict):
        return record
    return {k: v for k, v in record.items() if k in keys}


def _pick_list(records, keys):
    if not isinstance(records, list):
        return records
    return [_pick(record, keys) for record in records]


def project_state(state, *, stage):
    keys = STAGE1_STATE_KEYS if stage == 1 else STAGE2_STATE_KEYS
    projected = {k: v for k, v in state.items() if k in keys}
    projected["users"] = _pick_list(state.get("users", []), STAGE1_USER_KEYS)
    projected["payments"] = _pick_list(
        state.get("payments", []), STAGE1_PAYMENT_KEYS if stage == 1 else STAGE2_PAYMENT_KEYS
    )
    projected["requests"] = _pick_list(state.get("requests", []), STAGE1_REQUEST_KEYS)
    projected["settlements"] = _pick_list(state.get("settlements", []), STAGE1_SETTLEMENT_KEYS)
    projected["idempotency"] = _pick_list(state.get("idempotency", []), STAGE1_IDEM_KEYS)
    if stage >= 2:
        projected["authorizations"] = _pick_list(state.get("authorizations", []), STAGE2_AUTH_KEYS)
    return projected


def has_surplus(original, projected):
    """True when projecting dropped data at some level (i.e. the stage-3 extras)."""
    if isinstance(original, dict) and isinstance(projected, dict):
        if set(original) - set(projected):
            return True
        return any(has_surplus(original[k], projected[k]) for k in projected)
    if isinstance(original, list) and isinstance(projected, list):
        return any(has_surplus(a, b) for a, b in zip(original, projected))
    return False


# --------------------------------------------------------------------------- #
# Native stage-3 round trips
# --------------------------------------------------------------------------- #


def test_export_keeps_stage_header(api):
    snap = export(api)
    assert snap["track"] == "pocketful"
    assert snap["format_version"] == 1
    assert isinstance(snap["state"], dict)


def make_payment(api, token, amount=500):
    resp = api.post("/payments", token=token, idem=unique("pay"), body={"to_handle": "bob", "amount": amount})
    expect(resp, 201)
    return payload(resp)["payment_id"]


def test_roundtrip_is_lossless_for_stage3_state(api, tokens):
    pid = make_payment(api, tokens["ada"])
    correction(
        api, tokens["ada"], pid,
        expected_revision=1, amount=300, effective_at=seconds_from_now(-60), reason="fix",
    )
    first = export(api)
    expect(import_state(api, first), 204)
    second = export(api)
    assert second["state"] == first["state"]


def test_roundtrip_preserves_corrections(api, tokens):
    pid = make_payment(api, tokens["ada"])
    correction(
        api, tokens["ada"], pid,
        expected_revision=1, amount=300, effective_at=seconds_from_now(-60), reason="fix",
    )
    snap = export(api)
    expect(import_state(api, snap), 204)

    tok = relogin(api, "ada", "bob")
    items = payload(revisions(api, tok["ada"], pid))["revisions"]
    assert [r["revision"] for r in items] == [1, 2]
    assert items[1]["amount"] == 300 and items[1]["reason"] == "fix"

    entry = next(e for e in payload(statement(api, tok["ada"]))["entries"] if e["payment"]["payment_id"] == pid)
    assert entry["revision"] == 2
    assert entry["payment"]["amount"] == 300
    assert me(api, tok["ada"])["balance"] == 9700
    assert me(api, tok["bob"])["balance"] == 2800


def test_roundtrip_preserves_historical_balance(api, reset):
    reset(make_fixture(payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, created_at=T1)]))
    tok = relogin(api, "ada")
    assert payload(get_me(api, tok["ada"], as_of=BEFORE))["balance"] == 10500

    snap = export(api)
    expect(import_state(api, snap), 204)

    tok2 = relogin(api, "ada")
    assert payload(get_me(api, tok2["ada"], as_of=BEFORE))["balance"] == 10500
    assert payload(get_me(api, tok2["ada"], as_of=T1))["balance"] == 10000


def test_roundtrip_preserves_hold_timeline(api, reset):
    reset(
        make_fixture(
            authorizations=[
                seeded_authorization(
                    "a_1", "u_ada", "u_bob", 2000,
                    created_at=seconds_from_now(-7200), expires_at=seconds_from_now(7200),
                )
            ]
        )
    )
    tok = relogin(api, "ada")
    void_authorization(api, tok["ada"], "a_1")
    snap = export(api)
    expect(import_state(api, snap), 204)

    tok2 = relogin(api, "ada")
    record = find_auth(api, tok2["ada"], "a_1")
    assert record["status"] == "voided"
    assert record["closed_at"] is not None

    before = payload(get_me(api, tok2["ada"], as_of=seconds_from_now(-3600)))
    assert before["held"] == 2000 and before["available"] == 8000
    after = payload(get_me(api, tok2["ada"], as_of=seconds_from_now(3600)))
    assert after["held"] == 0 and after["available"] == 10000


def test_roundtrip_preserves_capture_in_statement(api, tokens):
    aid = payload(authorize(api, tokens["ada"], "bob", 1000))["authorization_id"]
    payment = payload(capture(api, tokens["bob"], aid, amount=400, final=False))
    snap = export(api)
    expect(import_state(api, snap), 204)

    tok = relogin(api, "ada", "bob")
    body = payload(statement(api, tok["ada"]))
    linked = [e for e in body["entries"] if e["payment"].get("authorization_id") == aid]
    assert len(linked) == 1
    assert linked[0]["payment"]["payment_id"] == payment["payment_id"]
    assert linked[0]["revision"] == 1
    assert me(api, tok["ada"])["held"] == 600


def test_roundtrip_preserves_correction_idempotency(api, tokens):
    pid = make_payment(api, tokens["ada"])
    key = unique("corr")
    effective_at = seconds_from_now(-60)
    first = payload(
        correction(
            api, tokens["ada"], pid,
            expected_revision=1, amount=300, effective_at=effective_at, reason="fix", key=key,
        )
    )
    snap = export(api)
    expect(import_state(api, snap), 204)

    replay = correction(
        api, tokens["ada"], pid,
        expected_revision=1, amount=300, effective_at=effective_at, reason="fix", key=key,
    )
    expect(replay, 200)
    assert payload(replay) == first
    assert len(payload(revisions(api, tokens["ada"], pid))["revisions"]) == 2


# --------------------------------------------------------------------------- #
# Upward compatibility: stage-1 and stage-2 exports
# --------------------------------------------------------------------------- #


def test_import_accepts_simulated_stage2_export(api, reset):
    reset(
        make_fixture(
            payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, created_at=T1)],
            authorizations=[
                seeded_authorization(
                    "a_1", "u_ada", "u_bob", 2000,
                    created_at=seconds_from_now(-7200), expires_at=seconds_from_now(7200),
                )
            ],
        )
    )
    live = export(api)
    older = dict(live)
    older["state"] = project_state(live["state"], stage=2)
    assert has_surplus(live["state"], older["state"]), "projection must strip stage-3 data"

    expect(import_state(api, older), 204)
    tok = relogin(api, "ada")
    view = me(api, tok["ada"])
    assert view["balance"] == 10000
    assert view["held"] == 2000
    assert payload(api.get("/authorizations", token=tok["ada"]))["authorizations"][0]["authorization_id"] == "a_1"

    items = payload(revisions(api, tok["ada"], "p_1"))["revisions"]
    assert len(items) == 1
    assert items[0]["revision"] == 1
    assert items[0]["effective_at"] == T1
    assert items[0]["recorded_at"] == T1

    entry = payload(statement(api, tok["ada"]))["entries"][0]
    assert entry["payment"]["payment_id"] == "p_1"
    assert entry["revision"] == 1


def test_import_accepts_simulated_stage1_export(api, reset):
    reset(
        make_fixture(
            payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, created_at=T1)],
            authorizations=[
                seeded_authorization(
                    "a_1", "u_ada", "u_bob", 2000,
                    created_at=seconds_from_now(-7200), expires_at=seconds_from_now(7200),
                )
            ],
        )
    )
    live = export(api)
    older = dict(live)
    older["state"] = project_state(live["state"], stage=1)
    assert has_surplus(live["state"], older["state"]), "projection must strip stage-2/3 data"

    expect(import_state(api, older), 204)
    tok = relogin(api, "ada", "bob")
    assert me(api, tok["ada"])["balance"] == 10000
    assert me(api, tok["ada"])["held"] == 0
    assert payload(api.get("/authorizations", token=tok["ada"]))["authorizations"] == []

    entries = payload(statement(api, tok["ada"]))["entries"]
    assert [e["payment"]["payment_id"] for e in entries] == ["p_1"]
    assert entries[0]["revision"] == 1
    assert entries[0]["effective_at"] == T1
    assert payload(get_me(api, tok["ada"], as_of=BEFORE))["balance"] == 10500


# --------------------------------------------------------------------------- #
# Session-scoped snapshots and failed imports
# --------------------------------------------------------------------------- #


def test_snapshot_survives_import_in_stage4(api, reset):
    # Stage 3 cleared snapshots on import; stage 4 exports and imports them, so
    # the token keeps paging its frozen entries after the import.
    reset(make_fixture(payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, created_at=T1)]))
    tok = relogin(api, "ada")
    first = payload(statement(api, tok["ada"]))
    token = first["snapshot"]

    snap = export(api)
    expect(import_state(api, snap), 204)
    paged = payload(statement(api, tok["ada"], snapshot=token))
    assert paged["entries"] == first["entries"]
    assert paged["opening_balance"] == first["opening_balance"]


def test_invalid_import_leaves_stage3_state_untouched(api, tokens):
    pid = make_payment(api, tokens["ada"])
    correction(
        api, tokens["ada"], pid,
        expected_revision=1, amount=300, effective_at=seconds_from_now(-60), reason="fix",
    )
    token = payload(statement(api, tokens["ada"]))["snapshot"]

    bad = {"track": "pocketful", "format_version": 2, "state": export(api)["state"]}
    expect(import_state(api, bad), 422, "validation_failed")
    expect(import_state(api, {"track": "pocketful", "format_version": 1, "state": 123}), 422, "validation_failed")

    assert len(payload(revisions(api, tokens["ada"], pid))["revisions"]) == 2
    assert me(api, tokens["ada"])["balance"] == 9700
    expect(statement(api, tokens["ada"], snapshot=token), 200)
