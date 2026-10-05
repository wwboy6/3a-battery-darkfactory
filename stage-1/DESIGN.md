# Pocketful — Stage 1 Design

## Context

- Spec: `/home/koda/Documents/dark-factory-ws4/dark-factory-wearedevs/pocketful/spec/stage-1.md`
- Track: `pocketful`
- Result repo: `./band-work/result`
- Stage folder: `./band-work/result/stage-1/`
- Deliverable: one containerized HTTP service only (no frontend, no Python package import).

## Stack (simplest that works)

- Language: Python 3.12
- HTTP: FastAPI + uvicorn, exactly **one worker** (single process keeps in-memory state coherent)
- Storage: **in-memory** (spec allows ephemeral state; no restart persistence required)
- Password hashing: stdlib `hashlib.scrypt` (no external password lib)
- Tokens: `secrets.token_urlsafe` (opaque, non-expiring, multiple per account)
- Concurrency: one module-level `asyncio.Lock` guarding all state reads+writes → no DB "locked" errors, deterministic under 50 in-flight requests
- Image: `python:3.12-slim`, install from `requirements.txt` at build time (build network available, runtime network absent)

## Folder layout (stage-1)

| Path | Owner | Contents |
|---|---|---|
| `stage-1/src/` | @dev | application code |
| `stage-1/Dockerfile` | @dev | container build |
| `stage-1/RUN.md` | @dev | build+run command, no manual setup |
| `stage-1/requirements.txt` | @dev | `fastapi`, `uvicorn` |
| `stage-1/tests/` | @tester | pytest HTTP tests |
| `stage-1/DESIGN.md` | @architect | this file |

## Modules (`src/`)

- `main.py` — FastAPI app, all routes, request validation, error mapping.
- `store.py` — state holder, atomic mutations, idempotency store, reset/import/export.
- `security.py` — password hash/verify, token issue/verify.
- `models.py` — dataclasses/pydantic for entities + fixture schema.

## State model

- `state.currency`, `state.minor_units`
- `User`: `id`, `email`, `password_hash`, `display_name`, `handle`, `balance`, `tokens` (set)
- `Payment`: `id`, `from_user_id`, `to_user_id`, `amount`, `currency`, `note`, `visibility`, `request_id|null`, `settlement_id|null`, `created_at`
- `Request`: `id`, `requester_id`, `payer_id`, `amount`, `currency`, `note`, `status`, `payment_id|null`, `created_at`
- `Settlement`: `id`, `committed_at`, `payment_ids` (order)
- `IdempotencyRecord`: `user_id`, `key`, `method`, `path`, `body` (canonical JSON), `response` (full original JSON), `status`
- IDs: opaque strings ≤64 chars; use `secrets.token_hex` or `u_`/`p_`/`rq_`/`sp_`/`st_` prefixes.

## Core invariants (enforced in `store.py` inside the lock)

1. Σ wallet balances always equals the total seeded by last `POST /_test/reset`.
2. No balance is ever negative, not even transiently (compute net before commit).
3. A request moves money at most once (`pending -> paid` only, inside the lock).
4. Payments and settlements are all-or-nothing atomic.

## Key flows

- **reset** — parse fixture, reject any negative `balance` with 422 and change nothing, else replace whole state atomically.
- **signup/login** — signup derives handle from email local part (lowercase, non `[a-z0-9_]` → `_`, truncate 20); handle collision → 409 `handle_taken` with no account created.
- **payment** — resolve idempotency → validate → atomically debit sender + credit receiver.
- **request lifecycle** — create (`pending`, no balance check) → pay (payer only, creates payment) → decline/cancel (author-specific, repeat of same terminal state is 200).
- **split** — equal shares (larger to earlier handle order, sum exact, 0 legal) → one `pending` request per non-caller participant.
- **feeds** — `GET /activity` (payments by visibility+participation), `GET /requests` (requester or payer only).
- **settlement** — operator only, 1..32 transfers, validate entries in input order then check collective affordability, commit all-or-nothing.
- **export/import** — export `{track, format_version:1, state}`; import atomically replaces and preserves credentials/tokens/permissions/idempotency.

## Idempotency semantics (§7)

- Scope: per authenticated user. Store key = `(method, path, canonical body)`.
- Resolution order after body parses as JSON object and auth succeeds: **idempotency check → endpoint field validation**.
- First use → 201; same key+body replay → 200 with original body (no side effects); same key+different body → 409 `idempotency_key_reuse`.
- Store **only successful (2xx) outcomes**; a 4xx failure leaves the key reusable.
- Required on exactly: `POST /payments`, `POST /requests`, `POST /requests/{id}/pay`, `POST /splits`, `POST /settlements`.
- Replay body identity is JSON value equality (`{}` ≠ `{"visibility":"public"}`).

## Validation & error notes (deviations matter)

- Amounts: accept JSON integral numbers `1000`, `1000.0`, `1e3`; reject strings/bools (`bool` is not a number). Parse then require integer value.
- Query params `limit`/`offset` must be plain decimal digits (`^[0-9]+$`); `1e9`, `4.0`, `+4` → 422.
- `note` non-string (incl `null`), bad `amount`, bad `visibility` → **422**, not 400 (spec override).
- Other wrong JSON field types / unparseable body → 400 `malformed_request`.
- Unknown request fields and query params are ignored.
- Timestamps: RFC 3339 with explicit offset (use UTC `+00:00`), seconds precision.
- Global error body `{"error":{"code":..., "message":...}}`.

## Requirement → component map

| Spec | Component |
|---|---|
| §2 delivery | `Dockerfile`, `RUN.md`, `requirements.txt` |
| §3 health/reset/conventions | `main.py` routes, `store.py` reset |
| §4 model/fixture/feed | `models.py`, `store.py` |
| §5 errors | `main.py` exception/validation mapping |
| §6 auth | `security.py`, `main.py` auth routes |
| §7 idempotency | `store.py` idempotency store |
| §8 API routes | `main.py` |
| §9 rounding | `store.py` split share computation |
| §10 export/import | `store.py` snapshot/replace |
| §11 settlements | `main.py` + `store.py` batch commit |

## Edge cases @tester must cover

- Concurrent identical idempotent writes → exactly one 201, rest 200, single money movement.
- Concurrent pay of same request → one `paid`, no negative balance, sum invariant holds.
- Request over payer balance stays `pending`, later becomes payable; pay while short → 409 and no change.
- Split rounding table incl `1/3 -> 1,0,0`, `999/3 -> 333,333,333`; order changes who gets the extra unit; `0` share still creates a request; caller-only split → empty `requests`.
- Signup handle derivation + `handle_taken` leaves no account; password `<8`; `email` shape; email taken 409.
- Note Unicode/emoji round trip byte-for-byte; note defaults `""`; visibility defaults.
- Reset with negative balance → 422 and unchanged; repeated reset; seeded login works immediately.
- Export→import→export equality; import preserves tokens, passwords, idempotency, permissions; invalid track/version → 422 unchanged; reset clears imported state.
- Settlement: non-operator 403, no-token 401, 1..32 transfers, entry-error precedence in input order, collective affordability, atomic all-or-nothing, replay returns original response.
- Activity visibility: public vs private vs third party; requests never in feed; `GET /requests` direction/status filters.
- JSON amount forms, strict integer query digits, `limit`/`offset` ranges, idempotency key length 1..255.

## Non-goals (do not build)

- Deposits, top-ups, withdrawals, cards, bank integrations.
- Directory/user search endpoints.
- Email verification, password reset, refresh tokens, role management.
- Follow graph, mute list; persistence across container restart.
- Any frontend/UI; multi-currency support.

