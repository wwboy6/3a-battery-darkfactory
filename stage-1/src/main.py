import json
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

import security
from models import ApiError, Settlement
from store import STATUSES, as_int, build_import_state, build_reset_state, now_iso, store

DIGITS_RE = re.compile(r"^[0-9]+$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
MAX_AMOUNT = 1000000000
MAX_NOTE = 200


class JsonResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"


app = FastAPI(default_response_class=JsonResponse)


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
    if not header:
        raise ApiError(401, "unauthenticated", "missing bearer token")
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise ApiError(401, "unauthenticated", "malformed bearer token")
    token = parts[1].strip()
    async with store.lock:
        uid = store.token_index.get(token)
        user = store.users.get(uid) if uid else None
    if user is None:
        raise ApiError(401, "unauthenticated", "unknown bearer token")
    return user


def current_user(user_id):
    user = store.users.get(user_id)
    if user is None:
        raise ApiError(401, "unauthenticated", "unknown bearer token")
    return user


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


@app.post("/auth/signup")
async def signup(request: Request):
    body = await read_json_object(request)
    email = required_key(body, "email")
    if not isinstance(email, str):
        raise ApiError(400, "malformed_request", "email must be a string")
    if not EMAIL_RE.match(email):
        raise ApiError(422, "validation_failed", "email must be of the form local@domain")
    password = required_key(body, "password")
    if not isinstance(password, str):
        raise ApiError(400, "malformed_request", "password must be a string")
    if len(password) < 8:
        raise ApiError(422, "validation_failed", "password must be at least 8 characters")
    display_name = required_key(body, "display_name")
    if not isinstance(display_name, str):
        raise ApiError(400, "malformed_request", "display_name must be a string")

    handle = derive_handle(email)
    password_hash = security.hash_password(password)
    async with store.lock:
        if email.strip().lower() in store.email_index:
            raise ApiError(409, "email_taken", "email already registered")
        if handle in store.handle_index:
            raise ApiError(409, "handle_taken", "handle already taken")
        user = store.create_user(email, password_hash, display_name, handle)
        token = store.issue_token(user)
    return JsonResponse(
        {"user_id": user.id, "display_name": user.display_name, "token": token}, status_code=201
    )


@app.post("/auth/login")
async def login(request: Request):
    body = await read_json_object(request)
    email = required_key(body, "email")
    password = required_key(body, "password")
    if not isinstance(email, str) or not isinstance(password, str):
        raise ApiError(400, "malformed_request", "email and password must be strings")
    async with store.lock:
        uid = store.email_index.get(email.strip().lower())
        user = store.users.get(uid) if uid else None
    if user is None or not security.verify_password(password, user.password_hash):
        raise ApiError(401, "unauthenticated", "invalid email or password")
    async with store.lock:
        current_user(user.id)
        token = store.issue_token(user)
    return {"user_id": user.id, "display_name": user.display_name, "token": token}


@app.get("/me")
async def me(request: Request):
    user = await authenticate(request)
    async with store.lock:
        user = current_user(user.id)
        return {
            "user_id": user.id,
            "display_name": user.display_name,
            "handle": user.handle,
            "balance": user.balance,
            "currency": store.currency,
            "minor_units": store.minor_units,
        }


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
        if user.balance < amount:
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
        if user.balance < existing.amount:
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
            if store.users[uid].balance + delta < 0:
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
