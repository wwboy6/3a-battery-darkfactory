"""Stage-4 export/import: refunds, correction batches, snapshots and upgrade.

The state blob is implementation-defined, so stage-1/2/3 exports are simulated
by projecting a live stage-4 export onto the older field sets. Projection drops
the stage-4-only data (refund_of links, correction_batch_id, batches, snapshots)
so the importer must derive the documented defaults.
"""

from support import (
    capture,
    correction_batch,
    correction_item,
    create_payment,
    expect,
    get_me,
    login,
    make_fixture,
    me,
    payload,
    refund,
    revisions,
    seconds_from_now,
    seeded_authorization,
    seeded_payment,
    statement,
    unique,
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


def operator_fixture(**kwargs):
    return make_fixture(settlement_operator_ids=["u_ada"], **kwargs)


def pay(api, token, to_handle="bob", amount=500):
    resp = create_payment(api, token, to_handle, amount)
    expect(resp, 201)
    return payload(resp)["payment_id"]


def find_payment(api, token, pid):
    items = payload(api.get("/activity", token=token))["payments"]
    return next(p for p in items if p["payment_id"] == pid)


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
STAGE3_STATE_KEYS = STAGE2_STATE_KEYS | {"revisions"}

USER_12_KEYS = {"id", "email", "password_hash", "display_name", "handle", "balance", "tokens"}
USER_3_KEYS = USER_12_KEYS | {"opening_balance"}
PAY_1_KEYS = {
    "id", "from_user_id", "to_user_id", "amount", "currency", "note",
    "visibility", "request_id", "settlement_id", "created_at",
}
PAY_2_KEYS = PAY_1_KEYS | {"authorization_id"}
AUTH_2_KEYS = {
    "id", "from_user_id", "to_user_id", "amount", "captured_amount", "currency",
    "note", "visibility", "status", "expires_at", "payment_id", "payment_ids",
    "created_at",
}
AUTH_3_KEYS = AUTH_2_KEYS | {"closed_at"}
REQUEST_KEYS = {
    "id", "requester_id", "payer_id", "amount", "currency", "note",
    "status", "payment_id", "created_at",
}
SETTLEMENT_KEYS = {"id", "committed_at", "payment_ids"}
IDEM_KEYS = {"user_id", "key", "method", "path", "body", "response", "status"}
REV_3_KEYS = {"payment_id", "revision", "amount", "effective_at", "recorded_at", "reason"}


def _pick(record, keys):
    if not isinstance(record, dict):
        return record
    return {k: v for k, v in record.items() if k in keys}


def _pick_list(records, keys):
    if not isinstance(records, list):
        return records
    return [_pick(record, keys) for record in records]


def project_state(state, *, stage):
    keys = {1: STAGE1_STATE_KEYS, 2: STAGE2_STATE_KEYS, 3: STAGE3_STATE_KEYS}[stage]
    projected = {k: v for k, v in state.items() if k in keys}
    projected["users"] = _pick_list(state.get("users", []), USER_3_KEYS if stage >= 3 else USER_12_KEYS)
    projected["payments"] = _pick_list(state.get("payments", []), PAY_2_KEYS if stage >= 2 else PAY_1_KEYS)
    projected["requests"] = _pick_list(state.get("requests", []), REQUEST_KEYS)
    projected["settlements"] = _pick_list(state.get("settlements", []), SETTLEMENT_KEYS)
    projected["idempotency"] = _pick_list(state.get("idempotency", []), IDEM_KEYS)
    if stage >= 2:
        projected["authorizations"] = _pick_list(
            state.get("authorizations", []), AUTH_3_KEYS if stage >= 3 else AUTH_2_KEYS
        )
    if stage >= 3:
        projected["revisions"] = _pick_list(state.get("revisions", []), REV_3_KEYS)
    return projected


def has_surplus(original, projected):
    if isinstance(original, dict) and isinstance(projected, dict):
        if set(original) - set(projected):
            return True
        return any(has_surplus(original[k], projected[k]) for k in projected)
    if isinstance(original, list) and isinstance(projected, list):
        return any(has_surplus(a, b) for a, b in zip(original, projected))
    return False


# --------------------------------------------------------------------------- #
# Native round trips
# --------------------------------------------------------------------------- #


def test_export_keeps_stage_header(api):
    snap = export(api)
    assert snap["track"] == "pocketful"
    assert snap["format_version"] == 1
    assert isinstance(snap["state"], dict)


def test_roundtrip_is_lossless_for_stage4_state(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob", "cy")
    p1 = pay(api, tok["ada"], "bob", 500)
    p2 = pay(api, tok["ada"], "cy", 400)
    refund(api, tok["bob"], p1, amount=200)
    correction_batch(api, tok["ada"], [correction_item(p2, amount=100, effective_at=seconds_from_now(-60))])
    payload(statement(api, tok["ada"]))  # mint a snapshot

    first = export(api)
    expect(import_state(api, first), 204)
    second = export(api)
    assert second["state"] == first["state"]


def test_roundtrip_preserves_refunds(api, tokens):
    original = pay(api, tokens["ada"], "bob", 500)
    refunded = payload(refund(api, tokens["bob"], original, amount=200))
    snap = export(api)
    expect(import_state(api, snap), 204)

    tok = relogin(api, "ada", "bob")
    row = find_payment(api, tok["bob"], refunded["payment_id"])
    assert row["refund_of"] == original
    assert row["from_user_id"] == "u_bob" and row["to_user_id"] == "u_ada"
    assert me(api, tok["ada"])["balance"] == 9700
    assert me(api, tok["bob"])["balance"] == 2800
    expect(refund(api, tok["bob"], original, amount=400), 422, "refund_exceeds_payment")


def test_roundtrip_preserves_correction_batches(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob", "cy")
    p1 = pay(api, tok["ada"], "bob", 500)
    p2 = pay(api, tok["ada"], "cy", 400)
    key = unique("batch")
    items = [
        correction_item(p1, amount=100, effective_at=seconds_from_now(-60)),
        correction_item(p2, amount=50, effective_at=seconds_from_now(-60)),
    ]
    batch = payload(correction_batch(api, tok["ada"], items, key=key))
    snap = export(api)
    expect(import_state(api, snap), 204)

    tok2 = relogin(api, "ada", "bob", "cy")
    rev = payload(revisions(api, tok2["ada"], p1))["revisions"][1]
    assert rev["correction_batch_id"] == batch["correction_batch_id"]
    entry = next(e for e in payload(statement(api, tok2["ada"]))["entries"] if e["payment"]["payment_id"] == p1)
    assert entry["payment"]["amount"] == 100

    replay = correction_batch(api, tok2["ada"], items, key=key)
    expect(replay, 200)
    assert payload(replay) == batch


def test_roundtrip_preserves_snapshots(api, reset):
    reset(make_fixture(payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, created_at=T1)]))
    tok = relogin(api, "ada")
    first = payload(statement(api, tok["ada"]))
    token = first["snapshot"]

    snap = export(api)
    expect(import_state(api, snap), 204)
    paged = payload(statement(api, tok["ada"], snapshot=token))
    assert paged["entries"] == first["entries"]
    assert paged["opening_balance"] == first["opening_balance"]

    reset(make_fixture())
    tok2 = relogin(api, "ada")
    expect(statement(api, tok2["ada"], snapshot=token), 404, "not_found")


# --------------------------------------------------------------------------- #
# Upward compatibility: stage-1/2/3 exports
# --------------------------------------------------------------------------- #


def _capture_fixture():
    return make_fixture(
        payments=[seeded_payment("p_1", "u_ada", "u_bob", 500, created_at=T1)],
        authorizations=[
            seeded_authorization(
                "a_1", "u_ada", "u_bob", 2000,
                created_at=seconds_from_now(-7200), expires_at=seconds_from_now(7200),
            )
        ],
    )


def test_import_accepts_simulated_stage3_export(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob", "cy")
    p1 = pay(api, tok["ada"], "bob", 500)
    p2 = pay(api, tok["ada"], "cy", 400)
    refund(api, tok["bob"], p1, amount=200)
    correction_batch(api, tok["ada"], [correction_item(p2, amount=100, effective_at=seconds_from_now(-60))])
    payload(statement(api, tok["ada"]))  # snapshot

    live = export(api)
    older = dict(live)
    older["state"] = project_state(live["state"], stage=3)
    assert has_surplus(live["state"], older["state"]), "projection must strip stage-4 data"

    expect(import_state(api, older), 204)
    tok2 = relogin(api, "ada", "bob", "cy")
    assert me(api, tok2["ada"])["balance"] == 9600  # refund + corrected batch preserved
    assert sum(me(api, tok2[h])["total"] for h in ("ada", "bob", "cy")) == 12500

    rev = payload(revisions(api, tok2["ada"], p2))["revisions"][1]
    assert rev["amount"] == 100
    assert rev.get("correction_batch_id") is None  # batch id defaulted away
    refund_row = next(
        p for p in payload(api.get("/activity", token=tok2["bob"]))["payments"]
        if p["from_user_id"] == "u_bob" and p["to_user_id"] == "u_ada" and p["amount"] == 200
    )
    assert refund_row.get("refund_of") is None


def test_import_accepts_simulated_stage2_export(api, reset):
    reset(_capture_fixture())
    tok = relogin(api, "ada", "bob")
    capture(api, tok["bob"], "a_1", amount=400, final=False)
    live = export(api)
    older = dict(live)
    older["state"] = project_state(live["state"], stage=2)
    assert has_surplus(live["state"], older["state"]), "projection must strip stage-3/4 data"

    expect(import_state(api, older), 204)
    tok2 = relogin(api, "ada", "bob")
    view = me(api, tok2["ada"])
    assert view["total"] == 9600
    assert view["held"] == 1600
    assert view["available"] == 8000
    assert payload(get_me(api, tok2["ada"], as_of=BEFORE))["balance"] == 10500

    entries = payload(statement(api, tok2["ada"]))["entries"]
    assert entries and all(e["revision"] == 1 for e in entries)
    assert entries[0]["effective_at"] == T1


def test_import_accepts_simulated_stage1_export(api, reset):
    reset(_capture_fixture())
    live = export(api)
    older = dict(live)
    older["state"] = project_state(live["state"], stage=1)
    assert has_surplus(live["state"], older["state"]), "projection must strip stage-2/3/4 data"

    expect(import_state(api, older), 204)
    tok = relogin(api, "ada", "bob")
    assert me(api, tok["ada"])["balance"] == 10000
    assert me(api, tok["ada"])["held"] == 0
    assert payload(api.get("/authorizations", token=tok["ada"]))["authorizations"] == []

    entries = payload(statement(api, tok["ada"]))["entries"]
    assert [e["payment"]["payment_id"] for e in entries] == ["p_1"]
    assert entries[0]["revision"] == 1
    assert entries[0]["effective_at"] == T1


# --------------------------------------------------------------------------- #
# Invalid imports
# --------------------------------------------------------------------------- #


def test_invalid_import_leaves_state_untouched(api, reset):
    reset(operator_fixture())
    tok = relogin(api, "ada", "bob", "cy")
    p1 = pay(api, tok["ada"], "bob", 500)
    p2 = pay(api, tok["ada"], "cy", 400)
    refund(api, tok["bob"], p1, amount=200)
    batch = payload(correction_batch(api, tok["ada"], [correction_item(p2, amount=100, effective_at=seconds_from_now(-60))]))
    token = payload(statement(api, tok["ada"]))["snapshot"]

    bad = {"track": "pocketful", "format_version": 2, "state": export(api)["state"]}
    expect(import_state(api, bad), 422, "validation_failed")
    expect(import_state(api, {"track": "pocketful", "format_version": 1, "state": 123}), 422, "validation_failed")

    rev = payload(revisions(api, tok["ada"], p2))["revisions"][1]
    assert rev["correction_batch_id"] == batch["correction_batch_id"]
    assert me(api, tok["ada"])["balance"] == 9600
    expect(statement(api, tok["ada"], snapshot=token), 200)
