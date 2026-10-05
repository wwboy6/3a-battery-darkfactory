"""§3.4/§5 conventions: numeric forms, strict query digits, error envelope."""

import pytest

from support import expect, payload, unique


def raw_payment(api, token, raw):
    return api.post("/payments", token=token, idem=unique("pay"), raw_body=raw)


# --------------------------------------------------------------------------- #
# Integral JSON number forms
# --------------------------------------------------------------------------- #


def test_integer_amount_form_is_accepted(api, tokens):
    resp = raw_payment(api, tokens["ada"], '{"to_handle": "bob", "amount": 1000}')
    expect(resp, 201)
    assert payload(resp)["amount"] == 1000


def test_float_integral_amount_form_is_accepted(api, tokens):
    resp = raw_payment(api, tokens["ada"], '{"to_handle": "bob", "amount": 1000.0}')
    expect(resp, 201)
    assert payload(resp)["amount"] == 1000


def test_exponent_amount_form_is_accepted(api, tokens):
    resp = raw_payment(api, tokens["ada"], '{"to_handle": "bob", "amount": 1e3}')
    expect(resp, 201)
    assert payload(resp)["amount"] == 1000


def test_float_that_is_integral_but_over_max_is_409_not_422(api, tokens):
    # 1e9 == 1000000000 exactly, so it is a valid amount; the caller just can't afford it.
    resp = raw_payment(api, tokens["ada"], '{"to_handle": "bob", "amount": 1e9}')
    expect(resp, 409, "insufficient_funds")


def test_fractional_amount_is_422(api, tokens):
    expect(raw_payment(api, tokens["ada"], '{"to_handle": "bob", "amount": 1000.5}'), 422, "validation_failed")


def test_string_amount_is_422(api, tokens):
    expect(raw_payment(api, tokens["ada"], '{"to_handle": "bob", "amount": "1000"}'), 422, "validation_failed")


def test_bool_amount_is_422(api, tokens):
    expect(raw_payment(api, tokens["ada"], '{"to_handle": "bob", "amount": true}'), 422, "validation_failed")


def test_integral_float_form_accepted_for_request_amount(api, tokens):
    resp = api.post("/requests", token=tokens["bob"], idem=unique("req"), raw_body='{"payer_handle": "ada", "amount": 1000.0}')
    expect(resp, 201)
    assert payload(resp)["amount"] == 1000


# --------------------------------------------------------------------------- #
# Strict decimal-digit query parameters
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["1e9", "4.0", "+4", "-1", "0", "201", "abc", "1 "])
def test_limit_invalid_forms_are_422(api, tokens, value):
    expect(api.get("/requests", token=tokens["ada"], params={"limit": value}), 422, "validation_failed")


@pytest.mark.parametrize("value", ["1", "50", "200"])
def test_limit_valid_values(api, tokens, value):
    expect(api.get("/requests", token=tokens["ada"], params={"limit": value}), 200)


@pytest.mark.parametrize("value", ["-1", "1e1", "+1", "abc", "1.0"])
def test_offset_invalid_forms_are_422(api, tokens, value):
    expect(api.get("/requests", token=tokens["ada"], params={"offset": value}), 422, "validation_failed")


@pytest.mark.parametrize("value", ["0", "1", "1000"])
def test_offset_valid_values(api, tokens, value):
    expect(api.get("/requests", token=tokens["ada"], params={"offset": value}), 200)


def test_activity_limit_and_offset_ranges(api, tokens):
    expect(api.get("/activity", token=tokens["ada"], params={"limit": "0"}), 422, "validation_failed")
    expect(api.get("/activity", token=tokens["ada"], params={"limit": "201"}), 422, "validation_failed")
    expect(api.get("/activity", token=tokens["ada"], params={"offset": "-3"}), 422, "validation_failed")
    expect(api.get("/activity", token=tokens["ada"], params={"limit": "200", "offset": "0"}), 200)


def test_unknown_query_parameters_are_ignored(api, tokens):
    expect(api.get("/activity", token=tokens["ada"], params={"limit": "5", "bogus": "x"}), 200)
    expect(api.get("/requests", token=tokens["ada"], params={"unknown": "1"}), 200)


# --------------------------------------------------------------------------- #
# Error envelope
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "method,path,status,code",
    [
        ("get", "/me", 401, "unauthenticated"),
        ("get", "/nonexistent", 404, "not_found"),
    ],
)
def test_error_envelope_shape(api, tokens, method, path, status, code):
    resp = api.get(path, token=tokens["ada"])
    assert resp.status_code == status, resp.text
    body = payload(resp)
    assert set(body) == {"error"}
    assert set(body["error"]) >= {"code", "message"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_unparseable_body_is_malformed_request(api, tokens):
    resp = api.post("/requests", token=tokens["bob"], idem=unique("req"), raw_body="{oops")
    expect(resp, 400, "malformed_request")


def test_wrong_field_type_is_malformed_request(api, tokens):
    resp = api.post("/requests", token=tokens["bob"], idem=unique("req"), body={"payer_handle": 5, "amount": 1})
    expect(resp, 400, "malformed_request")


# --------------------------------------------------------------------------- #
# Unknown fields ignored on every write endpoint
# --------------------------------------------------------------------------- #


def test_unknown_fields_ignored_on_requests(api, tokens):
    resp = api.post(
        "/requests",
        token=tokens["bob"],
        idem=unique("req"),
        body={"payer_handle": "ada", "amount": 5, "extra": [1, 2]},
    )
    expect(resp, 201)


def test_unknown_fields_ignored_on_splits(api, tokens):
    resp = api.post(
        "/splits",
        token=tokens["ada"],
        idem=unique("split"),
        body={"amount": 10, "participant_handles": ["ada", "bob"], "extra": {"x": 1}},
    )
    expect(resp, 201)


def test_unknown_fields_ignored_on_settlements(api, reset):
    from support import login, make_fixture
    reset(make_fixture(settlement_operator_ids=["u_ada"]))
    token = payload(login(api, "ada@example.com"))["token"]
    resp = api.post(
        "/settlements",
        token=token,
        idem=unique("stl"),
        body={"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 1}], "extra": 7},
    )
    expect(resp, 201)
