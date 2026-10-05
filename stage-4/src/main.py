import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

import security
from ledger import parse_instant, revision_json
from models import ApiError, Revision, Settlement
from store import (
    AUTHORIZATION_STATUSES,
    STATUSES,
    as_int,
    build_import_state,
    build_reset_state,
    now_iso,
    store,
)

DIGITS_RE = re.compile(r"^[0-9]+$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
MAX_AMOUNT = 1000000000
MAX_NOTE = 200


class JsonResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"


app = FastAPI(default_response_class=JsonResponse)

BASE_DIR = Path(__file__).resolve().parent.parent
COOKIE_NAME = "pocketful_token"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def money_filter(minor):
    if minor is None:
        minor = 0
    minor = int(minor)
    units = store.minor_units or 0
    currency = store.currency or ""
    negative = minor < 0
    value = abs(minor)
    if units == 0:
        text = str(value)
    else:
        scale = 10 ** units
        text = "{}.{:0{}d}".format(value // scale, value % scale, units)
    if negative:
        text = "-" + text
    return "{} {}".format(text, currency).strip()


templates.env.filters["money"] = money_filter


@app.exception_handler(ApiError)
async def _handle_api_error(request, exc):
    return JsonResponse(
        {"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status_code
    )


@app.exception_handler(StarletteHTTPException)
async def _handle_http_error(request, exc):
    codes = {
        400: "malformed_request",
        401: "unauthenticated",
        403: "forbidden",
        404: "not_found",
        405: "not_found",
        409: "conflict",
        422: "validation_failed",
    }
    code = codes.get(exc.status_code, "error")
    detail = exc.detail if isinstance(exc.detail, str) else code
    return JsonResponse({"error": {"code": code, "message": detail}}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def _handle_unexpected(request, exc):
    return JsonResponse(
        {"error": {"code": "internal_error", "message": "internal error"}}, status_code=500
    )


async def read_json_object(request):
    raw = await request.body()
    if not raw:
        raise ApiError(400, "malformed_request", "request body is required")
    try:
        data = json.loads(raw)
    except Exception:
        raise ApiError(400, "malformed_request", "body is not valid JSON")
    if not isinstance(data, dict):
        raise ApiError(400, "malformed_request", "body must be a JSON object")
    return data


def required_key(body, field):
    if field not in body:
        raise ApiError(422, "validation_failed", "{} is required".format(field))
    return body[field]


def required_handle(body, field):
    value = required_key(body, field)
    if not isinstance(value, str):
        raise ApiError(400, "malformed_request", "{} must be a string".format(field))
    return value


def parse_amount(body):
    amount = as_int(required_key(body, "amount"), "amount")
    if amount < 1 or amount > MAX_AMOUNT:
        raise ApiError(422, "validation_failed", "amount out of range")
    return amount


def parse_note(body):
    if "note" not in body:
        return ""
    value = body["note"]
    if not isinstance(value, str) or len(value) > MAX_NOTE:
        raise ApiError(422, "validation_failed", "invalid note")
    return value


def parse_visibility(body, default="public"):
    if "visibility" not in body:
        return default
    value = body["visibility"]
    if value not in ("public", "private"):
        raise ApiError(422, "validation_failed", "invalid visibility")
    return value


def idempotency_key(request):
    key = request.headers.get("Idempotency-Key")
    if key is None or key == "":
        raise ApiError(400, "missing_idempotency_key", "Idempotency-Key header is required")
    if len(key) > 255:
        raise ApiError(422, "validation_failed", "Idempotency-Key must be 1 to 255 characters")
    return key


async def authenticate(request):
    header = request.headers.get("Authorization")
    if header:
        parts = header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            raise ApiError(401, "unauthenticated", "malformed bearer token")
        token = parts[1].strip()
    else:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            raise ApiError(401, "unauthenticated", "missing credentials")
    async with store.lock:
        uid = store.token_index.get(token)
        user = store.users.get(uid) if uid else None
    if user is None:
        raise ApiError(401, "unauthenticated", "unknown credentials")
    return user


def current_user(user_id):
    user = store.users.get(user_id)
    if user is None:
        raise ApiError(401, "unauthenticated", "unknown bearer token")
    return user


def wants_html(request):
    return "text/html" in request.headers.get("accept", "").lower()


def is_form(request):
    ctype = request.headers.get("content-type", "").lower()
    return ctype.startswith("application/x-www-form-urlencoded") or ctype.startswith("multipart/form-data")


async def read_form(request):
    raw = await request.body()
    try:
        text = raw.decode("utf-8")
    except Exception:
        raise ApiError(400, "malformed_request", "invalid form body")
    data = parse_qs(text, keep_blank_values=True)
    return {key: values[0] for key, values in data.items()}


async def optional_user(request):
    try:
        return await authenticate(request)
    except ApiError:
        return None


def current_user_context(user):
    total = store.total(user)
    held = store.held(user.id)
    return {
        "user_id": user.id,
        "display_name": user.display_name,
        "handle": user.handle,
        "balance": total,
        "total": total,
        "available": total - held,
        "held": held,
        "currency": store.currency,
        "minor_units": store.minor_units,
    }


def render_page(request, name, context, status_code=200):
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def login_redirect():
    return RedirectResponse("/login", status_code=303)


def set_auth_cookie(response, token):
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", path="/")
    return response


def pagination(request):
    limit = 50
    offset = 0
    raw_limit = request.query_params.get("limit")
    raw_offset = request.query_params.get("offset")
    if raw_limit is not None:
        if not DIGITS_RE.match(raw_limit):
            raise ApiError(422, "validation_failed", "limit must be decimal digits")
        limit = int(raw_limit)
        if limit < 1 or limit > 200:
            raise ApiError(422, "validation_failed", "limit must be between 1 and 200")
    if raw_offset is not None:
        if not DIGITS_RE.match(raw_offset):
            raise ApiError(422, "validation_failed", "offset must be decimal digits")
        offset = int(raw_offset)
    return limit, offset


def derive_handle(email):
    local = email.split("@", 1)[0].lower()
    cleaned = "".join(
        ch if (ch.isascii() and (ch.isalnum() or ch == "_")) else "_" for ch in local
    )
    return cleaned[:20] or "_"


def split_shares(amount, count):
    base = amount // count
    remainder = amount % count
    return [base + (1 if index < remainder else 0) for index in range(count)]


def replay_or_conflict(user_id, key, method, path, body):
    record = store.idem_get(user_id, key, method, path)
    if record is None:
        return None
    if record.body == body:
        return JsonResponse(record.response, status_code=200)
    raise ApiError(409, "idempotency_key_reuse", "key reused with a different body")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/_test/reset")
async def reset(request: Request):
    fixture = await read_json_object(request)
    state = build_reset_state(fixture)
    async with store.lock:
        store._install(state)
    return Response(status_code=204)


@app.get("/_test/export")
async def export():
    async with store.lock:
        return store.export_state()


@app.post("/_test/import")
async def import_state(request: Request):
    payload = await read_json_object(request)
    state = build_import_state(payload)
    async with store.lock:
        store._install(state)
    return Response(status_code=204)


async def create_account(email, password, display_name):
    if not EMAIL_RE.match(email):
        raise ApiError(422, "validation_failed", "email must be of the form local@domain")
    if len(password) < 8:
        raise ApiError(422, "validation_failed", "password must be at least 8 characters")
    handle = derive_handle(email)
    password_hash = security.hash_password(password)
    async with store.lock:
        if email.strip().lower() in store.email_index:
            raise ApiError(409, "email_taken", "email already registered")
        if handle in store.handle_index:
            raise ApiError(409, "handle_taken", "handle already taken")
        user = store.create_user(email, password_hash, display_name, handle)
        token = store.issue_token(user)
    return user, token


async def authenticate_credentials(email, password):
    async with store.lock:
        uid = store.email_index.get(email.strip().lower())
        user = store.users.get(uid) if uid else None
    if user is None or not security.verify_password(password, user.password_hash):
        raise ApiError(401, "unauthenticated", "invalid email or password")
    async with store.lock:
        current_user(user.id)
        token = store.issue_token(user)
    return user, token


@app.post("/auth/signup")
async def signup(request: Request):
    if is_form(request):
        return await signup_form(request)
    body = await read_json_object(request)
    email = required_key(body, "email")
    if not isinstance(email, str):
        raise ApiError(400, "malformed_request", "email must be a string")
    password = required_key(body, "password")
    if not isinstance(password, str):
        raise ApiError(400, "malformed_request", "password must be a string")
    display_name = required_key(body, "display_name")
    if not isinstance(display_name, str):
        raise ApiError(400, "malformed_request", "display_name must be a string")
    user, token = await create_account(email, password, display_name)
    return JsonResponse(
        {"user_id": user.id, "display_name": user.display_name, "token": token}, status_code=201
    )


@app.post("/auth/login")
async def login(request: Request):
    if is_form(request):
        return await login_form(request)
    body = await read_json_object(request)
    email = required_key(body, "email")
    password = required_key(body, "password")
    if not isinstance(email, str) or not isinstance(password, str):
        raise ApiError(400, "malformed_request", "email and password must be strings")
    user, token = await authenticate_credentials(email, password)
    return {"user_id": user.id, "display_name": user.display_name, "token": token}


@app.get("/signup")
async def signup_page(request: Request):
    return render_page(request, "signup.html", {"error": None})


@app.post("/signup")
async def signup_form(request: Request):
    form = await read_form(request)
    email = form.get("email", "")
    password = form.get("password", "")
    display_name = form.get("display_name", "")
    try:
        _user, token = await create_account(email, password, display_name)
    except ApiError as exc:
        return render_page(request, "signup.html", {"error": exc.message}, status_code=200)
    return set_auth_cookie(RedirectResponse("/", status_code=303), token)


@app.get("/login")
async def login_page(request: Request):
    return render_page(request, "login.html", {"error": None})


@app.post("/login")
async def login_form(request: Request):
    form = await read_form(request)
    email = form.get("email", "")
    password = form.get("password", "")
    try:
        _user, token = await authenticate_credentials(email, password)
    except ApiError as exc:
        return render_page(request, "login.html", {"error": exc.message}, status_code=200)
    return set_auth_cookie(RedirectResponse("/", status_code=303), token)


@app.get("/logout")
@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/me")
async def me(request: Request):
    user = await authenticate(request)
    raw_as_of = request.query_params.get("as_of")
    raw_known_at = request.query_params.get("known_at")
    async with store.lock:
        user = current_user(user.id)
        response = {
            "user_id": user.id,
            "display_name": user.display_name,
            "handle": user.handle,
            "currency": store.currency,
            "minor_units": store.minor_units,
        }
        if raw_as_of is None and raw_known_at is None:
            total = store.total(user)
            held = store.held(user.id)
        else:
            as_of = parse_instant(raw_as_of, "as_of") if raw_as_of is not None else datetime.now(timezone.utc)
            known_at = parse_instant(raw_known_at, "known_at") if raw_known_at is not None else None
            total = store.total(user, as_of=as_of, known_at=known_at)
            held = store.held_at(user.id, as_of=as_of, known_at=known_at)
            if raw_as_of is not None:
                response["as_of"] = raw_as_of
            if raw_known_at is not None:
                response["known_at"] = raw_known_at
        response.update({"balance": total, "total": total, "available": total - held, "held": held})
        return response


@app.post("/payments")
async def create_payment(request: Request):
    user = await authenticate(request)
    key = idempotency_key(request)
    body = await read_json_object(request)
    path = request.url.path
    async with store.lock:
        user = current_user(user.id)
        replay = replay_or_conflict(user.id, key, "POST", path, body)
        if replay is not None:
            return replay
        amount = parse_amount(body)
        note = parse_note(body)
        visibility = parse_visibility(body)
        to_handle = required_handle(body, "to_handle")
        if to_handle == user.handle:
            raise ApiError(422, "self_payment", "cannot pay your own handle")
        to_uid = store.handle_index.get(to_handle)
        if to_uid is None:
            raise ApiError(404, "not_found", "no user with that handle")
        if store.available(user) < amount:
            raise ApiError(409, "insufficient_funds", "insufficient funds")
        payment = store.commit_payment(user, store.users[to_uid], amount, note, visibility)
        response = store.payment_json(payment)
        store.idem_put(user.id, key, "POST", path, body, response, 201)
    return JsonResponse(response, status_code=201)


@app.post("/requests")
async def create_request(request: Request):
    user = await authenticate(request)
    key = idempotency_key(request)
    body = await read_json_object(request)
    path = request.url.path
    async with store.lock:
        user = current_user(user.id)
        replay = replay_or_conflict(user.id, key, "POST", path, body)
        if replay is not None:
            return replay
        amount = parse_amount(body)
        note = parse_note(body)
        payer_handle = required_handle(body, "payer_handle")
        if payer_handle == user.handle:
            raise ApiError(422, "self_request", "cannot request money from yourself")
        payer_uid = store.handle_index.get(payer_handle)
        if payer_uid is None:
            raise ApiError(404, "not_found", "no user with that handle")
        new_request = store.create_request(user.id, payer_uid, amount, note)
        response = store.request_json(new_request)
        store.idem_put(user.id, key, "POST", path, body, response, 201)
    return JsonResponse(response, status_code=201)


@app.post("/requests/{request_id}/pay")
async def pay_request(request: Request, request_id: str):
    user = await authenticate(request)
    key = idempotency_key(request)
    body = await read_json_object(request)
    path = request.url.path
    async with store.lock:
        user = current_user(user.id)
        replay = replay_or_conflict(user.id, key, "POST", path, body)
        if replay is not None:
            return replay
        visibility = parse_visibility(body)
        existing = store.requests.get(request_id)
        if existing is None:
            raise ApiError(404, "not_found", "no such request")
        if existing.payer_id != user.id:
            raise ApiError(403, "forbidden", "only the payer may pay this request")
        if existing.status != "pending":
            raise ApiError(409, "request_not_pending", "request is not pending")
        if store.available(user) < existing.amount:
            raise ApiError(409, "insufficient_funds", "insufficient funds")
        requester = store.users[existing.requester_id]
        payment = store.commit_payment(
            user, requester, existing.amount, existing.note, visibility, request_id=existing.id
        )
        existing.status = "paid"
        existing.payment_id = payment.id
        response = store.payment_json(payment)
        store.idem_put(user.id, key, "POST", path, body, response, 201)
    return JsonResponse(response, status_code=201)


@app.post("/requests/{request_id}/decline")
async def decline_request(request: Request, request_id: str):
    user = await authenticate(request)
    async with store.lock:
        user = current_user(user.id)
        existing = store.requests.get(request_id)
        if existing is None:
            raise ApiError(404, "not_found", "no such request")
        if existing.payer_id != user.id:
            raise ApiError(403, "forbidden", "only the payer may decline this request")
        if existing.status == "declined":
            return store.request_json(existing)
        if existing.status != "pending":
            raise ApiError(409, "request_not_pending", "request is not pending")
        existing.status = "declined"
        return store.request_json(existing)


@app.post("/requests/{request_id}/cancel")
async def cancel_request(request: Request, request_id: str):
    user = await authenticate(request)
    async with store.lock:
        user = current_user(user.id)
        existing = store.requests.get(request_id)
        if existing is None:
            raise ApiError(404, "not_found", "no such request")
        if existing.requester_id != user.id:
            raise ApiError(403, "forbidden", "only the requester may cancel this request")
        if existing.status == "cancelled":
            return store.request_json(existing)
        if existing.status != "pending":
            raise ApiError(409, "request_not_pending", "request is not pending")
        existing.status = "cancelled"
        return store.request_json(existing)


@app.get("/requests")
async def list_requests(request: Request):
    if wants_html(request):
        return await requests_page(request)
    user = await authenticate(request)
    limit, offset = pagination(request)
    direction = request.query_params.get("direction")
    if direction is not None and direction not in ("incoming", "outgoing"):
        raise ApiError(422, "validation_failed", "invalid direction")
    status = request.query_params.get("status")
    if status is not None and status not in STATUSES:
        raise ApiError(422, "validation_failed", "invalid status")
    async with store.lock:
        user = current_user(user.id)
        items = []
        for rid in store.request_ids:
            item = store.requests[rid]
            if user.id not in (item.requester_id, item.payer_id):
                continue
            if direction == "incoming" and user.id != item.payer_id:
                continue
            if direction == "outgoing" and user.id != item.requester_id:
                continue
            if status is not None and item.status != status:
                continue
            items.append(item)
        items.sort(key=lambda item: item.created_at, reverse=True)
        page = items[offset: offset + limit]
        return {
            "requests": [store.request_json(item) for item in page],
            "has_more": len(items) > offset + limit,
        }


@app.post("/splits")
async def create_split(request: Request):
    user = await authenticate(request)
    key = idempotency_key(request)
    body = await read_json_object(request)
    path = request.url.path
    async with store.lock:
        user = current_user(user.id)
        replay = replay_or_conflict(user.id, key, "POST", path, body)
        if replay is not None:
            return replay
        amount = parse_amount(body)
        note = parse_note(body)
        handles = body.get("participant_handles")
        if handles is None:
            raise ApiError(422, "validation_failed", "participant_handles is required")
        if not isinstance(handles, list):
            raise ApiError(400, "malformed_request", "participant_handles must be a list")
        if not handles:
            raise ApiError(422, "validation_failed", "participant_handles must not be empty")
        if not all(isinstance(handle, str) for handle in handles):
            raise ApiError(400, "malformed_request", "participant_handles must be strings")
        if len(set(handles)) != len(handles):
            raise ApiError(422, "validation_failed", "duplicate participant handle")
        payer_ids = []
        for handle in handles:
            uid = store.handle_index.get(handle)
            if uid is None:
                raise ApiError(404, "not_found", "no user with that handle")
            payer_ids.append(uid)
        shares = split_shares(amount, len(handles))
        created_at = now_iso()
        shares_out = [{"handle": handles[i], "amount": shares[i]} for i in range(len(handles))]
        requests_out = []
        for index, handle in enumerate(handles):
            if payer_ids[index] == user.id:
                continue
            new_request = store.create_request(
                user.id, payer_ids[index], shares[index], note, created_at=created_at
            )
            requests_out.append(store.request_json(new_request))
        response = {
            "split_id": store.gen_id("sp_"),
            "amount": amount,
            "currency": store.currency,
            "note": note,
            "shares": shares_out,
            "requests": requests_out,
            "created_at": created_at,
        }
        store.idem_put(user.id, key, "POST", path, body, response, 201)
    return JsonResponse(response, status_code=201)


@app.get("/activity")
async def activity(request: Request):
    user = await authenticate(request)
    limit, offset = pagination(request)
    async with store.lock:
        user = current_user(user.id)
        items = []
        for pid in store.payment_ids:
            payment = store.payments[pid]
            if payment.visibility == "public" or user.id in (payment.from_user_id, payment.to_user_id):
                items.append(payment)
        items.sort(key=lambda payment: payment.created_at, reverse=True)
        page = items[offset: offset + limit]
        return {
            "payments": [store.payment_json(payment) for payment in page],
            "has_more": len(items) > offset + limit,
        }


@app.post("/settlements")
async def create_settlement(request: Request):
    user = await authenticate(request)
    key = idempotency_key(request)
    body = await read_json_object(request)
    path = request.url.path
    async with store.lock:
        user = current_user(user.id)
        replay = replay_or_conflict(user.id, key, "POST", path, body)
        if replay is not None:
            return replay
        if user.id not in store.operators:
            raise ApiError(403, "forbidden", "not a settlement operator")
        if "transfers" not in body:
            raise ApiError(422, "validation_failed", "transfers is required")
        transfers = body["transfers"]
        if not isinstance(transfers, list):
            raise ApiError(400, "malformed_request", "transfers must be a list")
        if not 1 <= len(transfers) <= 32:
            raise ApiError(422, "validation_failed", "transfers must contain 1 to 32 entries")
        parsed = []
        for transfer in transfers:
            if not isinstance(transfer, dict):
                raise ApiError(422, "validation_failed", "invalid transfer entry")
            amount = parse_amount(transfer)
            note = parse_note(transfer)
            visibility = parse_visibility(transfer)
            from_handle = required_handle(transfer, "from_handle")
            to_handle = required_handle(transfer, "to_handle")
            if from_handle == to_handle:
                raise ApiError(422, "self_payment", "self transfer")
            from_uid = store.handle_index.get(from_handle)
            if from_uid is None:
                raise ApiError(404, "not_found", "no user with that handle")
            to_uid = store.handle_index.get(to_handle)
            if to_uid is None:
                raise ApiError(404, "not_found", "no user with that handle")
            parsed.append((from_uid, to_uid, amount, note, visibility))

        net = {}
        for from_uid, to_uid, amount, _note, _visibility in parsed:
            net[from_uid] = net.get(from_uid, 0) - amount
            net[to_uid] = net.get(to_uid, 0) + amount
        for uid, delta in net.items():
            if store.available(store.users[uid]) + delta < 0:
                raise ApiError(409, "insufficient_funds", "insufficient collective funds")

        committed_at = now_iso()
        settlement_id = store.gen_id("st_")
        payments_out = []
        for from_uid, to_uid, amount, note, visibility in parsed:
            payment = store.commit_payment(
                store.users[from_uid],
                store.users[to_uid],
                amount,
                note,
                visibility,
                settlement_id=settlement_id,
                created_at=committed_at,
            )
            payments_out.append(payment)
        store.settlements[settlement_id] = Settlement(
            id=settlement_id,
            committed_at=committed_at,
            payment_ids=[payment.id for payment in payments_out],
        )
        response = {
            "settlement_id": settlement_id,
            "committed_at": committed_at,
            "payments": [store.payment_json(payment) for payment in payments_out],
        }
        store.idem_put(user.id, key, "POST", path, body, response, 201)
    return JsonResponse(response, status_code=201)


# --------------------------------------------------------------------------- #
# Authorizations API
# --------------------------------------------------------------------------- #


@app.post("/authorizations")
async def create_authorization(request: Request):
    user = await authenticate(request)
    key = idempotency_key(request)
    body = await read_json_object(request)
    path = request.url.path
    async with store.lock:
        user = current_user(user.id)
        replay = replay_or_conflict(user.id, key, "POST", path, body)
        if replay is not None:
            return replay
        amount = parse_amount(body)
        note = parse_note(body)
        visibility = parse_visibility(body)
        to_handle = required_handle(body, "to_handle")
        if to_handle == user.handle:
            raise ApiError(422, "self_payment", "cannot authorize your own handle")
        to_uid = store.handle_index.get(to_handle)
        if to_uid is None:
            raise ApiError(404, "not_found", "no user with that handle")
        if store.available(user) < amount:
            raise ApiError(409, "insufficient_funds", "insufficient funds")
        authorization = store.create_authorization(user, store.users[to_uid], amount, note, visibility)
        response = store.authorization_json(authorization)
        store.idem_put(user.id, key, "POST", path, body, response, 201)
    return JsonResponse(response, status_code=201)


@app.post("/authorizations/{authorization_id}/capture")
async def capture_authorization(request: Request, authorization_id: str):
    user = await authenticate(request)
    key = idempotency_key(request)
    body = await read_json_object(request)
    path = request.url.path
    async with store.lock:
        user = current_user(user.id)
        replay = replay_or_conflict(user.id, key, "POST", path, body)
        if replay is not None:
            return replay
        authorization = store.authorizations.get(authorization_id)
        if authorization is None:
            raise ApiError(404, "not_found", "no such authorization")
        if authorization.to_user_id != user.id:
            raise ApiError(403, "forbidden", "only the receiver may capture")
        status = store.effective_status(authorization)
        if status == "expired":
            raise ApiError(409, "authorization_expired", "authorization has expired")
        if status != "open":
            raise ApiError(409, "authorization_not_open", "authorization is not open")
        remaining = authorization.amount - authorization.captured_amount
        if "amount" in body:
            amount = as_int(body["amount"], "amount")
            if amount < 1:
                raise ApiError(422, "validation_failed", "amount must be at least 1")
        else:
            amount = remaining
        if amount > remaining:
            raise ApiError(422, "capture_exceeds_authorization", "amount exceeds the remaining authorization")
        final = body.get("final", True)
        if not isinstance(final, bool):
            raise ApiError(400, "malformed_request", "final must be a boolean")
        payer = store.users[authorization.from_user_id]
        receiver = store.users[authorization.to_user_id]
        payment = store.commit_payment(
            payer,
            receiver,
            amount,
            authorization.note,
            authorization.visibility,
            authorization_id=authorization.id,
        )
        authorization.captured_amount += amount
        authorization.payment_id = payment.id
        authorization.payment_ids.append(payment.id)
        if final or amount == remaining:
            authorization.status = "captured"
            authorization.closed_at = payment.created_at
        response = store.payment_json(payment)
        store.idem_put(user.id, key, "POST", path, body, response, 201)
    return JsonResponse(response, status_code=201)


@app.post("/authorizations/{authorization_id}/void")
async def void_authorization(request: Request, authorization_id: str):
    user = await authenticate(request)
    async with store.lock:
        user = current_user(user.id)
        authorization = store.authorizations.get(authorization_id)
        if authorization is None:
            raise ApiError(404, "not_found", "no such authorization")
        if authorization.from_user_id != user.id:
            raise ApiError(403, "forbidden", "only the payer may void")
        status = store.effective_status(authorization)
        if status == "voided":
            return store.authorization_json(authorization)
        if status in ("captured", "expired"):
            raise ApiError(409, "authorization_not_open", "authorization is not open")
        authorization.status = "voided"
        authorization.closed_at = now_iso()
        return store.authorization_json(authorization)


@app.get("/authorizations")
async def list_authorizations(request: Request):
    if wants_html(request):
        return await authorizations_page(request)
    user = await authenticate(request)
    limit, offset = pagination(request)
    direction = request.query_params.get("direction")
    if direction is not None and direction not in ("incoming", "outgoing"):
        raise ApiError(422, "validation_failed", "invalid direction")
    status = request.query_params.get("status")
    if status is not None and status not in AUTHORIZATION_STATUSES:
        raise ApiError(422, "validation_failed", "invalid status")
    async with store.lock:
        user = current_user(user.id)
        items = []
        for aid in store.authorization_ids:
            item = store.authorizations[aid]
            if user.id not in (item.from_user_id, item.to_user_id):
                continue
            if direction == "outgoing" and user.id != item.from_user_id:
                continue
            if direction == "incoming" and user.id != item.to_user_id:
                continue
            if status is not None and store.effective_status(item) != status:
                continue
            items.append(item)
        items.sort(key=lambda item: item.created_at, reverse=True)
        page = items[offset: offset + limit]
        return {
            "authorizations": [store.authorization_json(item) for item in page],
            "has_more": len(items) > offset + limit,
        }


# --------------------------------------------------------------------------- #
# Stage 3: statements and corrections
# --------------------------------------------------------------------------- #


@app.get("/statement")
async def statement(request: Request):
    user = await authenticate(request)
    limit, offset = pagination(request)
    raw_snapshot = request.query_params.get("snapshot")
    async with store.lock:
        user = current_user(user.id)
        if raw_snapshot is not None:
            if any(request.query_params.get(name) is not None for name in ("from", "to", "known_at")):
                raise ApiError(422, "validation_failed", "snapshot cannot be combined with from, to or known_at")
            snapshot = store.snapshots.get(raw_snapshot)
            if snapshot is None or snapshot["user_id"] != user.id:
                raise ApiError(404, "not_found", "unknown snapshot")
            opening = snapshot["opening_balance"]
            closing = snapshot["closing_balance"]
            entries = snapshot["entries"]
            token = raw_snapshot
        else:
            raw_from = request.query_params.get("from")
            raw_to = request.query_params.get("to")
            raw_known_at = request.query_params.get("known_at")
            from_dt = parse_instant(raw_from, "from") if raw_from is not None else None
            to_dt = parse_instant(raw_to, "to") if raw_to is not None else None
            known_at = parse_instant(raw_known_at, "known_at") if raw_known_at is not None else None
            opening, closing, entries = store.build_statement(user, from_dt, to_dt, known_at)
            token = store.create_snapshot(user.id, opening, closing, entries)
        return {
            "opening_balance": opening,
            "entries": entries[offset: offset + limit],
            "closing_balance": closing,
            "has_more": offset + limit < len(entries),
            "snapshot": token,
        }


@app.post("/payments/{payment_id}/corrections")
async def correct_payment(request: Request, payment_id: str):
    user = await authenticate(request)
    key = idempotency_key(request)
    body = await read_json_object(request)
    path = request.url.path
    async with store.lock:
        user = current_user(user.id)
        replay = replay_or_conflict(user.id, key, "POST", path, body)
        if replay is not None:
            return replay
        payment = store.payments.get(payment_id)
        if payment is None:
            raise ApiError(404, "not_found", "no such payment")
        if payment.from_user_id != user.id:
            raise ApiError(403, "forbidden", "only the original sender may correct")
        if (
            payment.settlement_id is not None
            or payment.authorization_id is not None
            or payment.refund_of is not None
        ):
            raise ApiError(422, "linked_payment_immutable", "linked payments cannot be corrected")
        expected_revision = as_int(required_key(body, "expected_revision"), "expected_revision")
        if expected_revision < 1:
            raise ApiError(422, "validation_failed", "expected_revision must be positive")
        amount = as_int(required_key(body, "amount"), "amount")
        if amount < 0 or amount > MAX_AMOUNT:
            raise ApiError(422, "validation_failed", "amount out of range")
        reason = required_key(body, "reason")
        if not isinstance(reason, str) or not (1 <= len(reason) <= 200):
            raise ApiError(422, "validation_failed", "reason must be 1 to 200 characters")
        effective_raw = required_key(body, "effective_at")
        effective_dt = parse_instant(effective_raw, "effective_at")
        if effective_dt > datetime.now(timezone.utc):
            raise ApiError(422, "validation_failed", "effective_at must not be in the future")
        revision = store.apply_correction(
            user, payment, expected_revision, amount, effective_raw, reason
        )
        response = {
            "payment_id": payment.id,
            "revision": revision.revision,
            "amount": revision.amount,
            "effective_at": revision.effective_at,
            "recorded_at": revision.recorded_at,
            "reason": revision.reason,
        }
        store.idem_put(user.id, key, "POST", path, body, response, 201)
    return JsonResponse(response, status_code=201)


@app.get("/payments/{payment_id}/revisions")
async def list_revisions(request: Request, payment_id: str):
    user = await authenticate(request)
    async with store.lock:
        user = current_user(user.id)
        payment = store.payments.get(payment_id)
        if payment is None or user.id not in (payment.from_user_id, payment.to_user_id):
            raise ApiError(404, "not_found", "no such payment")
        revisions = store.revisions.get(payment_id)
        if not revisions:
            revisions = [
                Revision(
                    revision=1,
                    amount=payment.amount,
                    effective_at=payment.created_at,
                    recorded_at=payment.created_at,
                    reason="",
                )
            ]
        return {"revisions": [revision_json(item) for item in revisions]}


# --------------------------------------------------------------------------- #
# Stage 4: refunds and correction batches
# --------------------------------------------------------------------------- #


@app.post("/payments/{payment_id}/refunds")
async def refund_payment(request: Request, payment_id: str):
    user = await authenticate(request)
    key = idempotency_key(request)
    body = await read_json_object(request)
    path = request.url.path
    async with store.lock:
        user = current_user(user.id)
        replay = replay_or_conflict(user.id, key, "POST", path, body)
        if replay is not None:
            return replay
        target = store.payments.get(payment_id)
        if target is None:
            raise ApiError(404, "not_found", "no such payment")
        if target.to_user_id != user.id:
            raise ApiError(403, "forbidden", "only the original receiver may refund")
        if target.refund_of is not None:
            raise ApiError(422, "invalid_refund_target", "cannot refund a refund")
        amount = as_int(required_key(body, "amount"), "amount")
        if amount < 1 or amount > MAX_AMOUNT:
            raise ApiError(422, "validation_failed", "amount out of range")
        latest = store.latest_revision(target.id)
        if store.refunded_total(target.id) + amount > latest.amount:
            raise ApiError(422, "refund_exceeds_payment", "refund exceeds the payment amount")
        receiver = store.users[target.to_user_id]
        if store.available(receiver) < amount:
            raise ApiError(409, "insufficient_funds", "insufficient funds")
        refund = store.commit_refund(receiver, store.users[target.from_user_id], amount, target)
        response = store.payment_json(refund)
        store.idem_put(user.id, key, "POST", path, body, response, 201)
    return JsonResponse(response, status_code=201)


@app.post("/correction-batches")
async def correction_batch(request: Request):
    user = await authenticate(request)
    key = idempotency_key(request)
    body = await read_json_object(request)
    path = request.url.path
    async with store.lock:
        user = current_user(user.id)
        replay = replay_or_conflict(user.id, key, "POST", path, body)
        if replay is not None:
            return replay
        if user.id not in store.operators:
            raise ApiError(403, "forbidden", "not a settlement operator")
        corrections = body.get("corrections")
        if corrections is None:
            raise ApiError(422, "validation_failed", "corrections is required")
        if not isinstance(corrections, list):
            raise ApiError(400, "malformed_request", "corrections must be a list")
        if not 1 <= len(corrections) <= 32:
            raise ApiError(422, "validation_failed", "corrections must contain 1 to 32 items")
        seen = set()
        for entry in corrections:
            if not isinstance(entry, dict):
                raise ApiError(422, "validation_failed", "invalid correction item")
            payment_id = entry.get("payment_id")
            if not isinstance(payment_id, str) or not payment_id:
                raise ApiError(422, "validation_failed", "payment_id is required")
            if payment_id in seen:
                raise ApiError(422, "validation_failed", "payment_ids must be distinct")
            seen.add(payment_id)
        batch_id, recorded_at, revisions = store.apply_correction_batch(corrections)
        response = {
            "correction_batch_id": batch_id,
            "recorded_at": recorded_at,
            "revisions": revisions,
        }
        store.idem_put(user.id, key, "POST", path, body, response, 201)
    return JsonResponse(response, status_code=201)


# --------------------------------------------------------------------------- #
# Server-rendered pages
# --------------------------------------------------------------------------- #


def _visible_payment_rows(user):
    rows = []
    for pid in store.payment_ids:
        payment = store.payments[pid]
        if payment.visibility == "public" or user.id in (payment.from_user_id, payment.to_user_id):
            rows.append(payment)
    rows.sort(key=lambda payment: payment.created_at, reverse=True)
    return [store.payment_json(payment) for payment in rows]


def _authorization_lists(user):
    incoming = []
    outgoing = []
    for aid in store.authorization_ids:
        authorization = store.authorizations[aid]
        if authorization.from_user_id == user.id:
            outgoing.append(authorization)
        elif authorization.to_user_id == user.id:
            incoming.append(authorization)
    incoming.sort(key=lambda item: item.created_at, reverse=True)
    outgoing.sort(key=lambda item: item.created_at, reverse=True)
    return (
        [store.authorization_json(item) for item in incoming],
        [store.authorization_json(item) for item in outgoing],
    )


@app.get("/")
async def index_page(request: Request):
    user = await optional_user(request)
    if user is None:
        return login_redirect()
    async with store.lock:
        user = current_user(user.id)
        context = {
            "current_user": current_user_context(user),
            "payments": _visible_payment_rows(user),
        }
    return render_page(request, "index.html", context)


async def requests_page(request):
    user = await optional_user(request)
    if user is None:
        return login_redirect()
    async with store.lock:
        user = current_user(user.id)
        incoming = []
        outgoing = []
        for rid in store.request_ids:
            item = store.requests[rid]
            if item.payer_id == user.id:
                incoming.append(item)
            elif item.requester_id == user.id:
                outgoing.append(item)
        incoming.sort(key=lambda item: item.created_at, reverse=True)
        outgoing.sort(key=lambda item: item.created_at, reverse=True)
        context = {
            "current_user": current_user_context(user),
            "incoming": [store.request_json(item) for item in incoming],
            "outgoing": [store.request_json(item) for item in outgoing],
        }
    return render_page(request, "requests.html", context)


async def authorizations_page(request):
    user = await optional_user(request)
    if user is None:
        return login_redirect()
    async with store.lock:
        user = current_user(user.id)
        incoming, outgoing = _authorization_lists(user)
        context = {
            "current_user": current_user_context(user),
            "incoming": incoming,
            "outgoing": outgoing,
        }
    return render_page(request, "authorizations.html", context)


@app.get("/split")
async def split_page(request: Request):
    user = await optional_user(request)
    if user is None:
        return login_redirect()
    async with store.lock:
        user = current_user(user.id)
        context = {"current_user": current_user_context(user)}
    return render_page(request, "split.html", context)
