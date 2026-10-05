import asyncio
import math
import re
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import security
from ledger import (
    effective_status,
    held_for,
    historical_overdraft,
    next_recorded_at,
    now_iso,
    parse_ts,
    remaining_amount,
    selected_map,
    total_for,
)
from models import (
    ApiError,
    Authorization,
    IdempotencyRecord,
    Payment,
    Request,
    Revision,
    Settlement,
    User,
)

HANDLE_RE = re.compile(r"^[a-z0-9_]{1,20}$")
VISIBILITIES = ("public", "private")
STATUSES = ("pending", "paid", "declined", "cancelled")
AUTHORIZATION_STATUSES = ("open", "captured", "voided", "expired")
DEFAULT_AUTHORIZATION_TTL = 600


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
        "revisions": {},
        "snapshots": {},
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
        if isinstance(created_at, str):
            try:
                created_dt = parse_ts(created_at)
            except Exception:
                raise ApiError(422, "validation_failed", "invalid created_at")
            if created_dt > datetime.now(timezone.utc):
                raise ApiError(422, "validation_failed", "created_at must not be in the future")
        else:
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
        state["revisions"][pid] = [
            Revision(
                revision=1,
                amount=amount,
                effective_at=created_at,
                recorded_at=created_at,
                reason="",
            )
        ]

    net = {uid: 0 for uid in state["users"]}
    for pid in state["payment_ids"]:
        payment = state["payments"][pid]
        net[payment.from_user_id] -= payment.amount
        net[payment.to_user_id] += payment.amount
    for uid, user in state["users"].items():
        user.opening_balance = user.balance - net[uid]

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
            closed_at=entry.get("closed_at") if isinstance(entry.get("closed_at"), str) else None,
        )
        state["authorization_ids"].append(aid)

    _validate_holds(state)

    return state


def _validate_holds(state):
    selected = selected_map(state.get("revisions", {}), None)
    held = {}
    for authorization in state["authorizations"].values():
        if effective_status(authorization) == "open":
            held[authorization.from_user_id] = held.get(authorization.from_user_id, 0) + (
                authorization.amount - authorization.captured_amount
            )
    for user_id, amount in held.items():
        user = state["users"][user_id]
        total = total_for(user.opening_balance, state["payments"], selected, user_id)
        if amount > total:
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

    raw_openings = {}
    for entry in users:
        if not isinstance(entry, dict):
            raise ApiError(422, "validation_failed", "invalid user")
        uid = _require(entry, "id")
        raw_openings[uid] = entry.get("opening_balance")
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

    raw_revisions = data.get("revisions")
    if raw_revisions is not None and not isinstance(raw_revisions, list):
        raise ApiError(422, "validation_failed", "invalid revisions")
    if isinstance(raw_revisions, list):
        for entry in raw_revisions:
            if not isinstance(entry, dict):
                raise ApiError(422, "validation_failed", "invalid revision")
            pid = _require(entry, "payment_id")
            if pid not in state["payments"]:
                raise ApiError(422, "validation_failed", "unknown revision payment")
            number = as_int(_require(entry, "revision"), "revision")
            amount = as_int(_require(entry, "amount"), "amount")
            effective_at = _require(entry, "effective_at")
            recorded_at = _require(entry, "recorded_at")
            reason = entry.get("reason", "")
            if not isinstance(effective_at, str) or not isinstance(recorded_at, str) or not isinstance(reason, str):
                raise ApiError(422, "validation_failed", "invalid revision")
            state["revisions"].setdefault(pid, []).append(
                Revision(
                    revision=number,
                    amount=amount,
                    effective_at=effective_at,
                    recorded_at=recorded_at,
                    reason=reason,
                )
            )
    for pid, payment in state["payments"].items():
        revisions = state["revisions"].setdefault(pid, [])
        if not revisions:
            revisions.append(
                Revision(
                    revision=1,
                    amount=payment.amount,
                    effective_at=payment.created_at,
                    recorded_at=payment.created_at,
                    reason="",
                )
            )
        revisions.sort(key=lambda item: item.revision)

    net = {uid: 0 for uid in state["users"]}
    for payment in state["payments"].values():
        net[payment.from_user_id] -= payment.amount
        net[payment.to_user_id] += payment.amount
    for uid, user in state["users"].items():
        supplied = raw_openings.get(uid)
        if supplied is not None:
            user.opening_balance = as_int(supplied, "opening_balance")
        else:
            user.opening_balance = user.balance - net[uid]

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
            closed_at=entry.get("closed_at") if isinstance(entry.get("closed_at"), str) else None,
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
        self.revisions = state["revisions"]
        self.snapshots = state["snapshots"]

    # ---- holds / availability (caller holds the lock) ----
    def effective_status(self, authorization):
        return effective_status(authorization)

    def remaining(self, authorization):
        return remaining_amount(authorization)

    def _all_authorizations(self):
        return [self.authorizations[aid] for aid in self.authorization_ids]

    def _captures_by_auth(self):
        captures = {}
        for pid in self.payment_ids:
            payment = self.payments[pid]
            if payment.authorization_id is not None:
                captures.setdefault(payment.authorization_id, []).append(payment)
        return captures

    def total(self, user, as_of=None, known_at=None, inclusive=True):
        selected = selected_map(self.revisions, known_at)
        return total_for(
            user.opening_balance, self.payments, selected, user.id, as_of=as_of, inclusive=inclusive
        )

    def held_at(self, user_id, as_of=None, known_at=None):
        return held_for(
            self._all_authorizations(), self._captures_by_auth(), user_id, as_of, known_at
        )

    def held(self, user_id):
        return self.held_at(user_id, as_of=datetime.now(timezone.utc))

    def available(self, user):
        return self.total(user) - self.held(user.id)

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
        self.revisions[pid] = [
            Revision(
                revision=1,
                amount=amount,
                effective_at=payment.created_at,
                recorded_at=payment.created_at,
                reason="",
            )
        ]
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
            "closed_at": self._closed_at(authorization),
        }

    def _closed_at(self, authorization):
        if effective_status(authorization) == "open":
            return None
        if authorization.closed_at:
            return authorization.closed_at
        if effective_status(authorization) == "expired":
            return authorization.expires_at
        return authorization.created_at

    def build_statement(self, user, from_dt, to_dt, known_at):
        selected = selected_map(self.revisions, known_at)
        if from_dt is None:
            opening = user.opening_balance
        else:
            opening = total_for(
                user.opening_balance, self.payments, selected, user.id, as_of=from_dt, inclusive=False
            )
        if to_dt is None:
            closing = total_for(user.opening_balance, self.payments, selected, user.id)
        else:
            closing = total_for(
                user.opening_balance, self.payments, selected, user.id, as_of=to_dt, inclusive=False
            )
        rows = []
        for pid, revision in selected.items():
            payment = self.payments.get(pid)
            if payment is None or user.id not in (payment.from_user_id, payment.to_user_id):
                continue
            effective = parse_ts(revision.effective_at)
            if from_dt is not None and effective < from_dt:
                continue
            if to_dt is not None and effective >= to_dt:
                continue
            delta = -revision.amount if payment.from_user_id == user.id else revision.amount
            rows.append((effective, pid, payment, revision, delta))
        rows.sort(key=lambda row: (row[0], row[1]))
        entries = []
        running = opening
        for _effective, _pid, payment, revision, delta in rows:
            running += delta
            payload = self.payment_json(payment)
            payload["amount"] = revision.amount
            entries.append(
                {
                    "payment": payload,
                    "delta": delta,
                    "balance_after": running,
                    "revision": revision.revision,
                    "effective_at": revision.effective_at,
                    "recorded_at": revision.recorded_at,
                }
            )
        return opening, closing, entries

    def create_snapshot(self, user_id, opening, closing, entries):
        token = secrets.token_urlsafe(24)
        self.snapshots[token] = {
            "user_id": user_id,
            "opening_balance": opening,
            "closing_balance": closing,
            "entries": entries,
        }
        return token

    def apply_correction(self, user, payment, expected_revision, amount, effective_at, reason):
        revisions = self.revisions.setdefault(payment.id, [])
        if not revisions:
            revisions.append(
                Revision(
                    revision=1,
                    amount=payment.amount,
                    effective_at=payment.created_at,
                    recorded_at=payment.created_at,
                    reason="",
                )
            )
        latest = revisions[-1]
        if expected_revision != latest.revision:
            raise ApiError(409, "stale_revision", "expected_revision is stale")
        delta = amount - latest.amount
        if delta > 0:
            if self.available(self.users[payment.from_user_id]) < delta:
                raise ApiError(409, "insufficient_funds", "insufficient funds")
        elif delta < 0:
            if self.available(self.users[payment.to_user_id]) < -delta:
                raise ApiError(409, "insufficient_funds", "insufficient funds")
        candidate = Revision(
            revision=latest.revision + 1,
            amount=amount,
            effective_at=effective_at,
            recorded_at=next_recorded_at(latest.recorded_at),
            reason=reason,
        )
        trial = {pid: list(items) for pid, items in self.revisions.items()}
        trial[payment.id] = revisions + [candidate]
        openings = {uid: item.opening_balance for uid, item in self.users.items()}
        if historical_overdraft(
            openings, self.payments, trial, self._all_authorizations(), self._captures_by_auth()
        ):
            raise ApiError(409, "historical_overdraft", "correction would overdraw a historical balance")
        revisions.append(candidate)
        return candidate

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
                        "balance": self.total(user),
                        "opening_balance": user.opening_balance,
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
                "revisions": [
                    {
                        "payment_id": pid,
                        "revision": revision.revision,
                        "amount": revision.amount,
                        "effective_at": revision.effective_at,
                        "recorded_at": revision.recorded_at,
                        "reason": revision.reason,
                    }
                    for pid, revisions in self.revisions.items()
                    for revision in revisions
                ],
                "operators": sorted(self.operators),
                "idempotency": [asdict(record) for record in self.idem.values()],
            },
        }


store = Store()
