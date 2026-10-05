"""Stage-1 export → stage-2 import: existing clients survive the upgrade.

The export's ``state`` object is implementation-defined, so the suite does not
hard-code its layout. It produces a faithful *stage-1-shaped* export by taking a
real stage-2 export and removing exactly the fields stage 2 added
(``authorizations``, ``authorization_ttl_seconds`` and each payment's
``authorization_id``). Import must accept that shape unchanged.
"""

from __future__ import annotations

from datetime import datetime

from support import (
    expect,
    make_fixture,
    me,
    parse_html,
    payload,
    require_testid,
    testid_text,
    unique,
)


def export(api):
    resp = api.get("/_test/export")
    expect(resp, 200)
    return payload(resp)


def import_state(api, obj):
    return api.post("/_test/import", body=obj)


def strip_stage2_fields(document):
    """Remove the stage-2 additions anywhere in the opaque export state."""
    removed = {"authorizations": 0, "authorization_ttl_seconds": 0, "authorization_id": 0}

    def walk(node):
        if isinstance(node, dict):
            for key in list(node.keys()):
                if key in removed:
                    removed[key] += 1
                    del node[key]
                else:
                    walk(node[key])
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(document)
    return removed


def stage1_shaped_export(api):
    snapshot = export(api)
    removed = strip_stage2_fields(snapshot)
    assert removed["authorizations"] >= 1, (
        "stage-2 export must include the authorizations array the stage-1 format omits"
    )
    assert removed["authorization_ttl_seconds"] >= 1, (
        "stage-2 export must include authorization_ttl_seconds"
    )
    return snapshot


def create_payment(api, token, body, key):
    return api.post("/payments", token=token, idem=key, body=body)


def create_authorization(api, token, body):
    resp = api.post("/authorizations", token=token, idem=unique("up-auth"), body=body)
    expect(resp, 201)
    return payload(resp)


def test_stage1_export_import_keeps_clients_and_retries(login_browser, api, tokens):
    ada, bob = tokens["ada"], tokens["bob"]
    browser, _ = login_browser("ada")  # the browser session that must survive

    # A pending request the imported client should still be able to pay.
    requested = api.post(
        "/requests",
        token=bob,
        idem=unique("up-req"),
        body={"payer_handle": "ada", "amount": 500, "note": "upgrade taxi"},
    )
    expect(requested, 201)
    rid = payload(requested)["request_id"]

    # A live hold: it exists only in stage 2 and must vanish with a stage-1 import.
    create_authorization(api, ada, {"to_handle": "bob", "amount": 1000})
    assert me(api, ada)["held"] == 1000

    # A payment that committed but whose response was lost to the client.
    lost_body = {"to_handle": "bob", "amount": 250, "note": "lost response"}
    lost_key = unique("up-lost")
    original = create_payment(api, ada, lost_body, lost_key)
    expect(original, 201)
    original_payment = payload(original)

    snapshot = stage1_shaped_export(api)
    expect(import_state(api, snapshot), 204)

    # The signed-in browser survives: its cookie still renders the wallet.
    page = browser.get("/", accept="text/html")
    expect(page, 200)
    document = parse_html(page.text)
    assert testid_text(document, "current-handle") == "ada"

    # The API session survives too, and the stage-1 export drops the hold.
    account = me(api, ada)
    assert account["total"] == 9750 and account["held"] == 0 and account["available"] == 9750

    # The pending request is still payable through the request screen.
    requests_page = parse_html(browser.get("/requests", accept="text/html").text)
    assert require_testid(requests_page, f"request-item-{rid}").attrs.get("data-status") == "pending"
    paid = api.post("/requests/%s/pay" % rid, token=ada, idem=unique("up-pay"), body={})
    expect(paid, 201)
    assert me(api, ada)["total"] == 9250
    assert me(api, bob)["total"] == 3250

    # The lost-response payment is retryable with the same key and body and
    # moves no extra money.
    retry = create_payment(api, ada, lost_body, lost_key)
    expect(retry, 200)
    assert payload(retry)["payment_id"] == original_payment["payment_id"]
    assert me(api, ada)["total"] == 9250

    # Payments imported from a stage-1 export carry no authorization_id.
    feed = api.get("/activity", token=ada)
    expect(feed, 200)
    assert payload(feed)["payments"]
    assert all(p["authorization_id"] is None for p in payload(feed)["payments"])


def test_stage1_import_drops_holds_and_restores_default_ttl(api, ada_token, reset):
    reset(make_fixture())
    create_authorization(api, ada_token, {"to_handle": "bob", "amount": 1500})
    snapshot = stage1_shaped_export(api)
    expect(import_state(api, snapshot), 204)

    # No authorizations survive a stage-1 export.
    listing = api.get("/authorizations", token=ada_token)
    expect(listing, 200)
    assert payload(listing)["authorizations"] == []
    assert me(api, ada_token)["held"] == 0

    # Missing authorization_ttl_seconds defaults to 600 for new holds.
    created = create_authorization(api, ada_token, {"to_handle": "bob", "amount": 100})
    delta = (
        datetime.fromisoformat(created["expires_at"])
        - datetime.fromisoformat(created["created_at"])
    ).total_seconds()
    assert delta == 600


def test_invalid_import_leaves_state_unchanged(api, ada_token):
    before = me(api, ada_token)
    expect(
        import_state(api, {"track": "pocketful", "format_version": 1}),
        422,
        "validation_failed",
    )
    expect(
        import_state(api, {"track": "pocketful", "format_version": 2, "state": {}}),
        422,
        "validation_failed",
    )
    expect(
        import_state(api, {"track": "other", "format_version": 1, "state": {}}),
        422,
        "validation_failed",
    )
    assert me(api, ada_token) == before
