"""§3 runtime contract: health and response conventions."""

import pytest

from support import assert_rfc3339, expect, payload, unique


def test_health_returns_200_status_ok_without_auth(api):
    resp = api.get("/health")
    expect(resp, 200)
    assert payload(resp) == {"status": "ok"}


@pytest.mark.parametrize("path", ["/health"])
def test_json_responses_use_utf8_charset(api, path):
    resp = api.get(path)
    content_type = resp.headers.get("Content-Type", "").lower()
    assert content_type.startswith("application/json"), content_type
    assert "charset=utf-8" in content_type, content_type


def test_authenticated_json_responses_use_utf8_charset(api, ada_token):
    resp = api.get("/me", token=ada_token)
    expect(resp, 200)
    content_type = resp.headers.get("Content-Type", "").lower()
    assert content_type.startswith("application/json"), content_type
    assert "charset=utf-8" in content_type, content_type


def test_timestamps_are_rfc3339_with_offset(api, tokens):
    resp = api.post(
        "/payments",
        token=tokens["ada"],
        idem=unique("pay"),
        body={"to_handle": "bob", "amount": 1},
    )
    expect(resp, 201)
    assert_rfc3339(payload(resp)["created_at"], "created_at")


def test_ids_are_at_most_64_chars(api, tokens):
    payment = payload(
        api.post("/payments", token=tokens["ada"], idem=unique("pay"), body={"to_handle": "bob", "amount": 1})
    )
    request = payload(
        api.post("/requests", token=tokens["bob"], idem=unique("req"), body={"payer_handle": "ada", "amount": 1})
    )
    split = payload(
        api.post(
            "/splits",
            token=tokens["ada"],
            idem=unique("split"),
            body={"amount": 2, "participant_handles": ["ada", "bob"]},
        )
    )
    for identifier in (payment["payment_id"], payment["from_user_id"], payment["to_user_id"],
                       request["request_id"], split["split_id"]):
        assert isinstance(identifier, str) and 0 < len(identifier) <= 64, repr(identifier)
