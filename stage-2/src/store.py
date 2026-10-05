import asyncio
import math
import re
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import security
from models import ApiError, Authorization, IdempotencyRecord, Payment, Request, Settlement, User

HANDLE_RE = re.compile(r"^[a-z0-9_]{1,20}$")
VISIBILITIES = ("public", "private")
STATUSES = ("pending", "paid", "declined", "cancelled")
AUTHORIZATION_STATUSES = ("open", "captured", "voided", "expired")
DEFAULT_AUTHORIZATION_TTL = 600


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def effective_status(authorization, now=None):
    if authorization.status == "open":
        now = now or datetime.now(timezone.utc)
        if parse_ts(authorization.expires_at) <= now:
            return "expired"
    return authorization.status


def remaining_amount(authorization, now=None):
    if effective_status(authorization, now) == "open":
        return authorization.amount - authorization.captured_amount
    return 0


def as_int(value, field="value") -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(422, "validation_failed", "{} must be an integer".format(field))
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ApiError(422, "validation_failed", "{} must be an integer".format(field))
        return int(value)
    return value


def _require(entry, field):
    if not isinstance(entry, dict) or field not in entry:
        raise ApiError(422, "validation_failed", "{} is required".format(field))
    return entry[field]


def _empty_state():
    return {
        "currency": None,
        "minor_units": None,
        "users": {},
        "handle_index": {},
        "email_index": {},
        "token_index": {},
        "payments": {},
        "payment_ids": [],
        "requests": {},
        "request_ids": [],
        "settlements": {},
        "authorization_ttl_seconds": DEFAULT_AUTHORIZATION_TTL,
        "authorizations": {},
        "authorization_ids": [],
        "operators": set(),
        "idem": {},
    }


def build_reset_state(fixture):
    """Validate a reset fixture and build a fresh state, or raise without side effects."""
    if not isinstance(fixture, dict):
        raise ApiError(400, "malformed_request", "body must be a JSON object")

    currency = fixture.get("currency")
    if not isinstance(currency, str) or not currency:
        raise ApiError(422, "validation_failed", "currency is required")
    minor_units = fixture.get("minor_units")
    if isinstance(minor_units, bool) or minor_units not in (0, 2, 3):
        raise ApiError(422, "validation_failed", "minor_units must be 0, 2 or 3")

    ttl = as_int(fixture.get("authorization_ttl_seconds", DEFAULT_AUTHORIZATION_TTL), "authorization_ttl_seconds")
    if ttl < 1:
        raise ApiError(422, "validation_failed", "authorization_ttl_seconds must be positive")

    raw_users = fixture.get("users")
    if not isinstance(raw_users, list):
        raise ApiError(422, "validation_failed", "users is required")

    state = _empty_state()
    state["currency"] = currency
    state["minor_units"] = minor_units

    for entry in raw_users:
        if not isinstance(entry, dict):
            raise ApiError(422, "validation_failed", "invalid user entry")
        uid = _require(entry, "id")
        email = _require(entry, "email")
        password = _require(entry, "password")
        display_name = _require(entry, "display_name")
        handle = _require(entry, "handle")
        balance = as_int(entry.get("balance", 0), "balance")
        if not isinstance(uid, str) or not uid:
            raise ApiError(422, "validation_failed", "invalid user id")
        if not isinstance(email, str) or not email:
            raise ApiError(422, "validation_failed", "invalid email")
        if not isinstance(password, str):
            raise ApiError(422, "validation_failed", "invalid password")
        if not isinstance(display_name, str):
            raise ApiError(422, "validation_failed", "invalid display_name")
        if not isinstance(handle, str) or not HANDLE_RE.match(handle):
            raise ApiError(422, "validation_failed", "invalid handle")
        if balance < 0:
            raise ApiError(422, "validation_failed", "balance must not be negative")
        email_key = email.strip().lower()
        if uid in state["users"] or handle in state["handle_index"] or email_key in state["email_index"]:
            raise ApiError(422, "validation_failed", "duplicate user")
        user = User(
            id=uid,
            email=email,
            password_hash=security.hash_password(password),
            display_name=display_name,
            handle=handle,
            balance=balance,
        )
        state["users"][uid] = user
        state["handle_index"][handle] = uid
        state["email_index"][email_key] = uid

    raw_payments = fixture.get("payments", [])
    if not isinstance(raw_payments, list):
        raise ApiError(422, "validation_failed", "payments must be a list")
    for entry in raw_payments:
        if not isinstance(entry, dict):
            raise ApiError(422, "validation_failed", "invalid payment entry")
        pid = _require(entry, "id")
        from_id = _require(entry, "from_user_id")
        to_id = _require(entry, "to_user_id")
        amount = as_int(_require(entry, "amount"), "amount")
        note = entry.get("note", "")
        visibility = entry.get("visibility", "public")
        created_at = entry.get("created_at")
        if not isinstance(pid, str) or not pid:
            raise ApiError(422, "validation_failed", "invalid payment id")
        if from_id not in state["users"] or to_id not in state["users"]:
            raise ApiError(422, "validation_failed", "unknown payment user")
        if amount < 0:
            raise ApiError(422, "validation_failed", "payment amount must not be negative")
        if not isinstance(note, str):
            raise ApiError(422, "validation_failed", "invalid note")
        if visibility not in VISIBILITIES:
            raise ApiError(422, "validation_failed", "invalid visibility")
        if not isinstance(created_at, str):
            created_at = now_iso()
        if pid in state["payments"]:
            raise ApiError(422, "validation_failed", "duplicate payment id")
        state["payments"][pid] = Payment(
            id=pid,
            from_user_id=from_id,
            to_user_id=to_id,
            amount=amount,
            currency=currency,
            note=note,
            visibility=visibility,
            request_id=entry.get("request_id"),
            settlement_id=entry.get("settlement_id"),
            created_at=created_at,
            authorization_id=entry.get("authorization_id"),
        )
        state["payment_ids"].append(pid)

    raw_requests = fixture.get("requests", [])
    if not isinstance(raw_requests, list):
        raise ApiError(422, "validation_failed", "requests must be a list")
    for entry in raw_requests:
        if not isinstance(entry, dict):
            raise ApiError(422, "validation_failed", "invalid request entry")
        rid = _require(entry, "id")
        requester_id = _require(entry, "requester_id")
        payer_id = _require(entry, "payer_id")
        amount = as_int(_require(entry, "amount"), "amount")
        note = entry.get("note", "")
        status = entry.get("status", "pending")
        created_at = entry.get("created_at")
        if not isinstance(rid, str) or not rid:
            raise ApiError(422, "validation_failed", "invalid request id")
        if requester_id not in state["users"] or payer_id not in state["users"]:
            raise ApiError(422, "validation_failed", "unknown request user")
        if amount < 0:
            raise ApiError(422, "validation_failed", "request amount must not be negative")
        if not isinstance(note, str):
            raise ApiError(422, "validation_failed", "invalid note")
        if status not in STATUSES:
            raise ApiError(422, "validation_failed", "invalid status")
        if not isinstance(created_at, str):
            created_at = now_iso()
        if rid in state["requests"]:
            raise ApiError(422, "validation_failed", "duplicate request id")
        state["requests"][rid] = Request(
            id=rid,
            requester_id=requester_id,
            payer_id=payer_id,
            amount=amount,
            currency=currency,
            note=note,
            status=status,
            payment_id=entry.get("payment_id"),
            created_at=created_at,
        )
        state["request_ids"].append(rid)

    operators = fixture.get("settlement_operator_ids", [])
    if not isinstance(operators, list) or not all(
        isinstance(op, str) and op in state["users"] for op in operators
    ):
        raise ApiError(422, "validation_failed", "invalid settlement_operator_ids")
    state["operators"] = set(operators)

    state["authorization_ttl_seconds"] = ttl
    raw_authorizations = fixture.get("authorizations", [])
    if not isinstance(raw_authorizations, list):
        raise ApiError(422, "validation_failed", "authorizations must be a list")
    for entry in raw_authorizations:
        if not isinstance(entry, dict):
            raise ApiError(422, "validation_failed", "invalid authorization entry")
        aid = _require(entry, "id")
        from_id = _require(entry, "from_user_id")
        to_id = _require(entry, "to_user_id")
        amount = as_int(_require(entry, "amount"), "amount")
        expires_at = _require(entry, "expires_at")
        captured_amount = as_int(entry.get("captured_amount", 0), "captured_amount")
        note = entry.get("note", "")
        visibility = entry.get("visibility", "public")
        status = entry.get("status", "open")
        payment_id = entry.get("payment_id")
        payment_ids = entry.get("payment_ids", [])
        if not isinstance(aid, str) or not aid or aid in state["authorizations"]:
            raise ApiError(422, "validation_failed", "invalid authorization id")
        if from_id not in state["users"] or to_id not in state["users"]:
            raise ApiError(422, "validation_failed", "unknown authorization user")
        if amount < 0 or captured_amount < 0 or captured_amount > amount:
            raise ApiError(422, "validation_failed", "invalid authorization amount")
        if not isinstance(note, str) or visibility not in VISIBILITIES:
            raise ApiError(422, "validation_failed", "invalid authorization fields")
        if status not in AUTHORIZATION_STATUSES:
            raise ApiError(422, "validation_failed", "invalid authorization status")
        if not isinstance(expires_at, str):
            raise ApiError(422, "validation_failed", "invalid expires_at")
        if not isinstance(payment_ids, list):
            raise ApiError(422, "validation_failed", "invalid payment_ids")
        state["authorizations"][aid] = Authorization(
            id=aid,
            from_user_id=from_id,
            to_user_id=to_id,
            amount=amount,
            captured_amount=captured_amount,
            currency=currency,
            note=note,
            visibility=visibility,
            status=status,
            expires_at=expires_at,
            payment_id=payment_id,
            payment_ids=list(payment_ids),
            created_at=entry.get("created_at") if isinstance(entry.get("created_at"), str) else now_iso(),
        )
        state["authorization_ids"].append(aid)

    _validate_holds(state)

    return state


def _validate_holds(state):
    held = {}
    for authorization in state["authorizations"].values():
        if effective_status(authorization) == "open":
            held[authorization.from_user_id] = held.get(authorization.from_user_id, 0) + (
                authorization.amount - authorization.captured_amount
            )
    for user_id, amount in held.items():
        if amount > state["users"][user_id].balance:
            raise ApiError(422, "validation_failed", "open holds exceed the user balance")


def build_import_state(payload):
    if not isinstance(payload, dict):
        raise ApiError(400, "malformed_request", "body must be a JSON object")
    if payload.get("track") != "pocketful" or payload.get("format_version") != 1:
        raise ApiError(422, "validation_failed", "invalid export header")
    state = payload.get("state")
    if not isinstance(state, dict):
        raise ApiError(422, "validation_failed", "state is required")
    try:
        return _state_from_export(state)
    except ApiError:
        raise
    except Exception:
        raise ApiError(422, "validation_failed", "invalid state")


def _state_from_export(data):
    currency = data.get("currency")
    minor_units = data.get("minor_units")
    if not isinstance(currency, str) or not currency:
        raise ApiError(422, "validation_failed", "invalid currency")
    if isinstance(minor_units, bool) or minor_units not in (0, 2, 3):
        raise ApiError(422, "validation_failed", "invalid minor_units")

    ttl = as_int(data.get("authorization_ttl_seconds", DEFAULT_AUTHORIZATION_TTL), "authorization_ttl_seconds")
    if ttl < 1:
        raise ApiError(422, "validation_failed", "invalid authorization_ttl_seconds")

    users = data.get("users")
    if not isinstance(users, list):
        raise ApiError(422, "validation_failed", "invalid users")

    state = _empty_state()
    state["currency"] = currency
    state["minor_units"] = minor_units

    for entry in users:
        if not isinstance(entry, dict):
            raise ApiError(422, "validation_failed", "invalid user")
        uid = _require(entry, "id")
        handle = _require(entry, "handle")
        email = _require(entry, "email")
        password_hash = _require(entry, "password_hash")
        display_name = _require(entry, "display_name")
        balance = as_int(entry.get("balance", 0), "balance")
        if not all(isinstance(x, str) for x in (uid, handle, email, password_hash, display_name)):
            raise ApiError(422, "validation_failed", "invalid user field")
        if balance < 0:
            raise ApiError(422, "validation_failed", "balance must not be negative")
        if uid in state["users"] or handle in state["handle_index"]:
            raise ApiError(422, "validation_failed", "duplicate user")
        user = User(
            id=uid,
            email=email,
            password_hash=password_hash,
            display_name=display_name,
            handle=handle,
            balance=balance,
        )
        tokens = entry.get("tokens", [])
        if not isinstance(tokens, list):
            raise ApiError(422, "validation_failed", "invalid tokens")
        for token in tokens:
            if not isinstance(token, str):
                raise ApiError(422, "validation_failed", "invalid token")
            user.tokens.add(token)
            state["token_index"][token] = uid
        state["users"][uid] = user
        state["handle_index"][handle] = uid
        state["email_index"][email.strip().lower()] = uid

    for entry in data.get("payments", []) or []:
        if not isinstance(entry, dict):
            raise ApiError(422, "validation_failed", "invalid payment")
        pid = _require(entry, "id")
        from_id = _require(entry, "from_user_id")
        to_id = _require(entry, "to_user_id")
        amount = as_int(_require(entry, "amount"), "amount")
        note = entry.get("note", "")
        visibility = entry.get("visibility", "public")
        created_at = entry.get("created_at")
        if not isinstance(pid, str) or not isinstance(note, str):
            raise ApiError(422, "validation_failed", "invalid payment")
        if visibility not in VISIBILITIES or not isinstance(created_at, str):
            raise ApiError(422, "validation_failed", "invalid payment")
        if from_id not in state["users"] or to_id not in state["users"]:
            raise ApiError(422, "validation_failed", "unknown payment user")
        state["payments"][pid] = Payment(
            id=pid,
            from_user_id=from_id,
            to_user_id=to_id,
            amount=amount,
            currency=currency,
            note=note,
            visibility=visibility,
            request_id=entry.get("request_id"),
            settlement_id=entry.get("settlement_id"),
            created_at=created_at,
            authorization_id=entry.get("authorization_id"),
        )

    for entry in data.get("authorizations", []) or []:
        if not isinstance(entry, dict):
            raise ApiError(422, "validation_failed", "invalid authorization")
        aid = _require(entry, "id")
        from_id = _require(entry, "from_user_id")
        to_id = _require(entry, "to_user_id")
        amount = as_int(_require(entry, "amount"), "amount")
        expires_at = _require(entry, "expires_at")
        captured_amount = as_int(entry.get("captured_amount", 0), "captured_amount")
        note = entry.get("note", "")
        visibility = entry.get("visibility", "public")
        status = entry.get("status", "open")
        payment_ids = entry.get("payment_ids", [])
        if not isinstance(aid, str) or not aid or aid in state["authorizations"]:
            raise ApiError(422, "validation_failed", "invalid authorization id")
        if from_id not in state["users"] or to_id not in state["users"]:
            raise ApiError(422, "validation_failed", "unknown authorization user")
        if amount < 0 or captured_amount < 0 or captured_amount > amount:
            raise ApiError(422, "validation_failed", "invalid authorization amount")
        if not isinstance(note, str) or visibility not in VISIBILITIES:
            raise ApiError(422, "validation_failed", "invalid authorization fields")
        if status not in AUTHORIZATION_STATUSES or not isinstance(expires_at, str):
            raise ApiError(422, "validation_failed", "invalid authorization status")
        if not isinstance(payment_ids, list):
            raise ApiError(422, "validation_failed", "invalid payment_ids")
        state["authorizations"][aid] = Authorization(
            id=aid,
            from_user_id=from_id,
            to_user_id=to_id,
            amount=amount,
            captured_amount=captured_amount,
            currency=currency,
            note=note,
            visibility=visibility,
            status=status,
            expires_at=expires_at,
            payment_id=entry.get("payment_id"),
            payment_ids=list(payment_ids),
            created_at=entry.get("created_at") if isinstance(entry.get("created_at"), str) else now_iso(),
        )

    for entry in data.get("requests", []) or []:
        if not isinstance(entry, dict):
            raise ApiError(422, "validation_failed", "invalid request")
        rid = _require(entry, "id")
        requester_id = _require(entry, "requester_id")
        payer_id = _require(entry, "payer_id")
        amount = as_int(_require(entry, "amount"), "amount")
        note = entry.get("note", "")
        status = entry.get("status", "pending")
        created_at = entry.get("created_at")
        if not isinstance(rid, str) or not isinstance(note, str):
            raise ApiError(422, "validation_failed", "invalid request")
        if status not in STATUSES or not isinstance(created_at, str):
            raise ApiError(422, "validation_failed", "invalid request")
        if requester_id not in state["users"] or payer_id not in state["users"]:
            raise ApiError(422, "validation_failed", "unknown request user")
        state["requests"][rid] = Request(
            id=rid,
            requester_id=requester_id,
            payer_id=payer_id,
            amount=amount,
            currency=currency,
            note=note,
            status=status,
            payment_id=entry.get("payment_id"),
            created_at=created_at,
        )

    for entry in data.get("settlements", []) or []:
        if not isinstance(entry, dict):
            raise ApiError(422, "validation_failed", "invalid settlement")
        sid = _require(entry, "id")
        committed_at = _require(entry, "committed_at")
        payment_ids = entry.get("payment_ids", [])
        if not isinstance(sid, str) or not isinstance(committed_at, str) or not isinstance(payment_ids, list):
            raise ApiError(422, "validation_failed", "invalid settlement")
        state["settlements"][sid] = Settlement(id=sid, committed_at=committed_at, payment_ids=list(payment_ids))

    operators = data.get("operators", []) or []
    if not isinstance(operators, list) or not all(
        isinstance(op, str) and op in state["users"] for op in operators
    ):
        raise ApiError(422, "validation_failed", "invalid operators")
    state["operators"] = set(operators)

    for entry in data.get("idempotency", []) or []:
        if not isinstance(entry, dict):
            raise ApiError(422, "validation_failed", "invalid idempotency record")
        user_id = _require(entry, "user_id")
        key = _require(entry, "key")
        method = _require(entry, "method")
        path = _require(entry, "path")
        if not all(isinstance(x, str) for x in (user_id, key, method, path)):
            raise ApiError(422, "validation_failed", "invalid idempotency record")
        state["idem"][(user_id, key, method, path)] = IdempotencyRecord(
            user_id=user_id,
            key=key,
            method=method,
            path=path,
            body=entry.get("body"),
            response=entry.get("response"),
            status=entry.get("status"),
        )

    state["payment_ids"] = _ordered_ids(data.get("payment_order"), state["payments"])
    state["request_ids"] = _ordered_ids(data.get("request_order"), state["requests"])
    state["authorization_ids"] = _ordered_ids(data.get("authorization_order"), state["authorizations"])
    state["authorization_ttl_seconds"] = ttl
    _validate_holds(state)
    return state


def _ordered_ids(order, mapping):
    result = []
    if isinstance(order, list):
        for item in order:
            if isinstance(item, str) and item in mapping and item not in result:
                result.append(item)
    for item in mapping:
        if item not in result:
            result.append(item)
    return result


class Store:
    def __init__(self):
        self.lock = asyncio.Lock()
        self._install(_empty_state())

    def _install(self, state):
        self.currency = state["currency"]
        self.minor_units = state["minor_units"]
        self.users = state["users"]
        self.handle_index = state["handle_index"]
        self.email_index = state["email_index"]
        self.token_index = state["token_index"]
        self.payments = state["payments"]
        self.payment_ids = state["payment_ids"]
        self.requests = state["requests"]
        self.request_ids = state["request_ids"]
        self.settlements = state["settlements"]
        self.authorization_ttl_seconds = state["authorization_ttl_seconds"]
        self.authorizations = state["authorizations"]
        self.authorization_ids = state["authorization_ids"]
        self.operators = state["operators"]
        self.idem = state["idem"]

    # ---- holds / availability (caller holds the lock) ----
    def effective_status(self, authorization):
        return effective_status(authorization)

    def remaining(self, authorization):
        return remaining_amount(authorization)

    def held(self, user_id):
        total = 0
        for aid in self.authorization_ids:
            authorization = self.authorizations[aid]
            if authorization.from_user_id == user_id:
                total += remaining_amount(authorization)
        return total

    def available(self, user):
        return user.balance - self.held(user.id)

    def gen_id(self, prefix):
        return prefix + secrets.token_hex(8)

    def create_user(self, email, password_hash, display_name, handle, balance=0):
        uid = self.gen_id("u_")
        user = User(
            id=uid,
            email=email,
            password_hash=password_hash,
            display_name=display_name,
            handle=handle,
            balance=balance,
        )
        self.users[uid] = user
        self.handle_index[handle] = uid
        self.email_index[email.strip().lower()] = uid
        return user

    def issue_token(self, user):
        token = security.new_token()
        user.tokens.add(token)
        self.token_index[token] = user.id
        return token

    def create_request(self, requester_id, payer_id, amount, note, created_at=None):
        rid = self.gen_id("rq_")
        request = Request(
            id=rid,
            requester_id=requester_id,
            payer_id=payer_id,
            amount=amount,
            currency=self.currency,
            note=note,
            status="pending",
            payment_id=None,
            created_at=created_at or now_iso(),
        )
        self.requests[rid] = request
        self.request_ids.append(rid)
        return request

    def create_authorization(self, from_user, to_user, amount, note, visibility):
        aid = self.gen_id("a_")
        created_at = now_iso()
        expires_at = (parse_ts(created_at) + timedelta(seconds=self.authorization_ttl_seconds)).isoformat()
        authorization = Authorization(
            id=aid,
            from_user_id=from_user.id,
            to_user_id=to_user.id,
            amount=amount,
            captured_amount=0,
            currency=self.currency,
            note=note,
            visibility=visibility,
            status="open",
            expires_at=expires_at,
            payment_id=None,
            payment_ids=[],
            created_at=created_at,
        )
        self.authorizations[aid] = authorization
        self.authorization_ids.append(aid)
        return authorization

    def commit_payment(self, from_user, to_user, amount, note, visibility, request_id=None, settlement_id=None, created_at=None, authorization_id=None):
        pid = self.gen_id("p_")
        from_user.balance -= amount
        to_user.balance += amount
        payment = Payment(
            id=pid,
            from_user_id=from_user.id,
            to_user_id=to_user.id,
            amount=amount,
            currency=self.currency,
            note=note,
            visibility=visibility,
            request_id=request_id,
            settlement_id=settlement_id,
            created_at=created_at or now_iso(),
            authorization_id=authorization_id,
        )
        self.payments[pid] = payment
        self.payment_ids.append(pid)
        return payment

    def idem_get(self, user_id, key, method, path):
        return self.idem.get((user_id, key, method, path))

    def idem_put(self, user_id, key, method, path, body, response, status):
        self.idem[(user_id, key, method, path)] = IdempotencyRecord(
            user_id=user_id,
            key=key,
            method=method,
            path=path,
            body=body,
            response=response,
            status=status,
        )

    def payment_json(self, payment):
        from_user = self.users.get(payment.from_user_id)
        to_user = self.users.get(payment.to_user_id)
        result = {
            "payment_id": payment.id,
            "from_user_id": payment.from_user_id,
            "from_handle": from_user.handle if from_user else None,
            "to_user_id": payment.to_user_id,
            "to_handle": to_user.handle if to_user else None,
            "amount": payment.amount,
            "currency": payment.currency,
            "note": payment.note,
            "visibility": payment.visibility,
            "request_id": payment.request_id,
            "authorization_id": payment.authorization_id,
            "created_at": payment.created_at,
        }
        if payment.settlement_id is not None:
            result["settlement_id"] = payment.settlement_id
        return result

    def request_json(self, request):
        requester = self.users.get(request.requester_id)
        payer = self.users.get(request.payer_id)
        return {
            "request_id": request.id,
            "requester_id": request.requester_id,
            "requester_handle": requester.handle if requester else None,
            "payer_id": request.payer_id,
            "payer_handle": payer.handle if payer else None,
            "amount": request.amount,
            "currency": request.currency,
            "note": request.note,
            "status": request.status,
            "payment_id": request.payment_id,
            "created_at": request.created_at,
        }

    def authorization_json(self, authorization):
        from_user = self.users.get(authorization.from_user_id)
        to_user = self.users.get(authorization.to_user_id)
        return {
            "authorization_id": authorization.id,
            "from_user_id": authorization.from_user_id,
            "from_handle": from_user.handle if from_user else None,
            "to_user_id": authorization.to_user_id,
            "to_handle": to_user.handle if to_user else None,
            "amount": authorization.amount,
            "captured_amount": authorization.captured_amount,
            "remaining_amount": remaining_amount(authorization),
            "currency": authorization.currency,
            "note": authorization.note,
            "visibility": authorization.visibility,
            "status": effective_status(authorization),
            "expires_at": authorization.expires_at,
            "payment_id": authorization.payment_id,
            "payment_ids": list(authorization.payment_ids),
            "created_at": authorization.created_at,
        }

    def export_state(self):
        return {
            "track": "pocketful",
            "format_version": 1,
            "state": {
                "currency": self.currency,
                "minor_units": self.minor_units,
                "users": [
                    {
                        "id": user.id,
                        "email": user.email,
                        "password_hash": user.password_hash,
                        "display_name": user.display_name,
                        "handle": user.handle,
                        "balance": user.balance,
                        "tokens": sorted(user.tokens),
                    }
                    for user in self.users.values()
                ],
                "payments": [asdict(payment) for payment in self.payments.values()],
                "payment_order": list(self.payment_ids),
                "requests": [asdict(request) for request in self.requests.values()],
                "request_order": list(self.request_ids),
                "settlements": [asdict(settlement) for settlement in self.settlements.values()],
                "authorization_ttl_seconds": self.authorization_ttl_seconds,
                "authorizations": [asdict(authorization) for authorization in self.authorizations.values()],
                "authorization_order": list(self.authorization_ids),
                "operators": sorted(self.operators),
                "idempotency": [asdict(record) for record in self.idem.values()],
            },
        }


store = Store()
