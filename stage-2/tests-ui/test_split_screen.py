"""The split screen: preview must match the server's rounding rule."""

from playwright.sync_api import expect

from support import path_of, payload


def _is_split_post(request) -> bool:
    return request.method == "POST" and path_of(request.url) == "/splits"


def _fill_split(page, amount, handles, note="dinner"):
    page.get_by_test_id("split-amount").fill(amount)
    page.get_by_test_id("split-handles").fill(handles)
    page.get_by_test_id("split-note").fill(note)


def _preview(page, handle):
    return page.get_by_test_id(f"split-share-{handle}").inner_text().strip()


def test_preview_matches_server_shares(signed_in):
    page = signed_in("ada")
    page.goto("/split")
    _fill_split(page, "10.00", "ada,bob,cy")
    expect(page.get_by_test_id("split-share-ada")).to_have_text("3.34 EUR")
    expect(page.get_by_test_id("split-share-bob")).to_have_text("3.33 EUR")
    expect(page.get_by_test_id("split-share-cy")).to_have_text("3.33 EUR")
    preview = {handle: _preview(page, handle) for handle in ("ada", "bob", "cy")}

    with page.expect_response(_is_split_post) as info:
        page.get_by_test_id("split-submit").click()
    body = payload(info.value)
    assert body["amount"] == 1000
    shares = {share["handle"]: share["amount"] for share in body["shares"]}
    assert shares == {"ada": 334, "bob": 333, "cy": 333}
    assert preview == {"ada": "3.34 EUR", "bob": "3.33 EUR", "cy": "3.33 EUR"}


def test_extra_minor_unit_goes_to_first_participant_in_order(signed_in):
    page = signed_in("ada")
    page.goto("/split")
    _fill_split(page, "10.00", "cy,bob,ada")
    expect(page.get_by_test_id("split-share-cy")).to_have_text("3.34 EUR")
    expect(page.get_by_test_id("split-share-bob")).to_have_text("3.33 EUR")
    expect(page.get_by_test_id("split-share-ada")).to_have_text("3.33 EUR")


def test_uneven_division_of_999(signed_in):
    page = signed_in("ada")
    page.goto("/split")
    _fill_split(page, "9.99", "ada,bob,cy")
    for handle in ("ada", "bob", "cy"):
        expect(page.get_by_test_id(f"split-share-{handle}")).to_have_text("3.33 EUR")


def test_preview_updates_when_amount_changes(signed_in):
    page = signed_in("ada")
    page.goto("/split")
    _fill_split(page, "10.00", "ada,bob")
    expect(page.get_by_test_id("split-share-ada")).to_have_text("5.00 EUR")
    page.get_by_test_id("split-amount").fill("9.00")
    expect(page.get_by_test_id("split-share-ada")).to_have_text("4.50 EUR")
    expect(page.get_by_test_id("split-share-bob")).to_have_text("4.50 EUR")


def test_preview_is_shown_before_any_submission(signed_in):
    page = signed_in("ada")
    page.goto("/split")
    posted = []
    page.on("request", lambda request: posted.append(request) if _is_split_post(request) else None)
    _fill_split(page, "10.00", "ada,bob")
    expect(page.get_by_test_id("split-preview")).to_be_visible()
    expect(page.get_by_test_id("split-share-ada")).to_be_visible()
    page.wait_for_timeout(200)
    assert posted == []


def test_invalid_amount_shows_error_without_request(signed_in):
    page = signed_in("ada")
    page.goto("/split")
    posted = []
    page.on("request", lambda request: posted.append(request) if _is_split_post(request) else None)
    _fill_split(page, "15.005", "ada,bob")
    page.get_by_test_id("split-submit").click()
    expect(page.get_by_test_id("split-error")).to_be_visible()
    page.wait_for_timeout(300)
    assert posted == []


def test_empty_handles_shows_error(signed_in):
    page = signed_in("ada")
    page.goto("/split")
    _fill_split(page, "10.00", "")
    page.get_by_test_id("split-submit").click()
    expect(page.get_by_test_id("split-error")).to_be_visible()


def test_unknown_handle_shows_error(signed_in):
    page = signed_in("ada")
    page.goto("/split")
    _fill_split(page, "10.00", "ada,nobody")
    page.get_by_test_id("split-submit").click()
    expect(page.get_by_test_id("split-error")).to_be_visible()


def test_split_creates_requests_for_other_participants(signed_in):
    page = signed_in("ada")
    page.goto("/split")
    _fill_split(page, "10.00", "ada,bob,cy")
    with page.expect_response(_is_split_post) as info:
        page.get_by_test_id("split-submit").click()
    body = payload(info.value)
    request_ids = [request["request_id"] for request in body["requests"]]
    assert len(request_ids) == 2

    page.goto("/requests")
    outgoing = page.get_by_test_id("outgoing-list")
    for request_id in request_ids:
        expect(outgoing.get_by_test_id(f"request-item-{request_id}")).to_be_visible()
