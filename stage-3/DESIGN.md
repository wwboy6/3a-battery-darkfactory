# Pocketful — Stage 3 Design

## Context & baseline

- Spec: `/home/koda/Documents/dark-factory-ws4/dark-factory-wearedevs/pocketful/spec/stage-3.md`
- Stages 1 and 2 continue to apply. Baseline: `band-work/result/stage-2/` (copied into `stage-3/`).
- Stage folder: `./band-work/result/stage-3/`
- Stage-1 spec: `/home/koda/Documents/dark-factory-ws4/dark-factory-wearedevs/pocketful/spec/stage-1.md`
- Stage-2 spec: `/home/koda/Documents/dark-factory-ws4/dark-factory-wearedevs/pocketful/spec/stage-2.md`
- **Backend-only stage.** No new screens or `data-testid`; the stage-2 UI must keep working unchanged.

## Stack

- Unchanged: FastAPI + uvicorn (single worker), in-memory state, one `asyncio.Lock`, Jinja2 SSR.
- No new dependencies. Keep `format_version: 1`.

## Core design: a temporal ledger

Replace "balance is a stored int" with a derived ledger, so historical views stay consistent.

- `User` gains `opening_balance` (int). Current `total`/`balance` = opening + net effect of all
  selected payment revisions.
- `Payment` keeps its original receipt fields (`amount` = revision-1 amount, `created_at`,
  parties, note, visibility, `request_id`, `settlement_id`, `authorization_id`). The activity feed
  still shows this original receipt; corrections are not feed items.
- New `Revision` record per payment: `revision` (1..n), `amount`, `effective_at`, `recorded_at`,
  `reason`. Revision 1 is `amount = original`, `effective_at = recorded_at = created_at`,
  `reason = ""`. A correction appends an immutable revision; `recorded_at` strictly increases.
- Money movement for a revision: original sender −amount, original receiver +amount (parties and
  visibility never change). Only the latest revision per payment is ever selected for a view.

## Ledger selection rules

- Without `known_at`: select each payment's latest recorded revision, then apply by `effective_at`.
- With `known_at` (RFC 3339 instant, optional on `/me` and `/statement`): select the latest
  revision whose `recorded_at <= known_at`; a payment with no such revision contributes nothing.
- `as_of` (inclusive) on `/me`: include selected revisions with `effective_at <= as_of`.
- `/statement` uses the half-open window `[from, to)` on selected `effective_at`.

## Opening balances & reset

- `opening_balance = seeded balance − Σ(net effect of seeded payments' revision 1)`. Loading
  seeded payments never changes the seeded `balance`.
- New accounts open at 0. Seeded history is consistent and nonnegative.
- Seeded payments may supply `created_at`; omission = reset time (before API-created payments).
  A seeded `created_at` in the future → 422 `validation_failed`, no change.

## API additions (dev)

- `GET /me?as_of=&known_at=`: both optional RFC 3339 instants with offset; anything else
  (naive/bare date/empty) → 422. Echo supplied values exactly. Without params, keep the current
  shape and current corrected values (no `as_of`/`known_at` keys).
- `GET /statement?from=&to=&limit=&offset=&snapshot=`: `from` defaults to opening, `to` to now,
  half-open; entries oldest first by `effective_at` then payment `id`; each entry has `payment`,
  `delta`, `balance_after`, `revision`, `effective_at`, `recorded_at`; `payment.amount` is the
  selected amount; zero-amount revisions appear with delta 0. `opening_balance`/`closing_balance`
  describe the full window and are pagination-independent. Only the caller's sent/received
  payments appear (feed visibility rules do not apply).
- `POST /payments/{payment_id}/corrections` (idempotent): original sender only (403/404/401
  otherwise). Body `{expected_revision, amount, effective_at, reason}` all required.
  - `expected_revision` positive int; `amount` int 0..1e9 (0 reverses the payment); `reason`
    1..200 chars; `effective_at` RFC 3339 not later than now. Invalid → 422.
  - Stale `expected_revision` → 409 `stale_revision`. Replay → 200 with the original revision,
    even after newer revisions. Different body same key → 409 `idempotency_key_reuse`.
  - Returns 201 with `payment_id`, `revision`, `amount`, `effective_at`, `recorded_at`, `reason`.
  - Settlement members and captures are immutable → 422 `linked_payment_immutable`
    (identified by `settlement_id != null` or `authorization_id != null`).
- `GET /payments/{payment_id}/revisions`: `{"revisions":[...]}` revision order incl. revision 1
  (`reason: ""`); only the two parties (third party 404 even if public, no token 401).

## Correction money math & safety

- The delta from the previous amount moves between the same two wallets atomically. Increase
  debits the original sender; decrease debits the original receiver.
- First check the immediate debit against the debited party's current `available` → 409
  `insufficient_funds`.
- Then replay the full historical stream with the new revision applied: if any user's corrected
  `total` or `available` is negative at any effective/event boundary → 409 `historical_overdraft`.
  Balances at a boundary include all movements at that instant. Current unaffordable debit takes
  precedence over `historical_overdraft`.
- Failure changes nothing: balances, revision history, statements and idempotency state preserved.
  Sum of balances equals the seeded total in every historical view.

## Stable statement pagination

- First `GET /statement` (no `snapshot`) returns an opaque `snapshot` token freezing the caller's
  selected revisions, window, opening/closing balances, entries and default `to`.
- `GET /statement?snapshot=&limit=&offset=` pages that frozen result even after later payments or
  corrections. Only `limit`/`offset` may accompany it; `from`/`to`/`known_at` with it → 422.
- Unknown token, another user's token, or a token from before reset → 404. Tokens last until reset
  (cleared on reset and import). Paging must keep `balance_after`, opening/closing and `has_more`
  correct; offset beyond the end returns an empty final page with `has_more: false`.
- Concurrent corrections with the same `expected_revision` cannot both succeed (lock and revision
  monotonicity guarantee this).

## Settlement & capture history

- Settlement members keep their original receipt/privacy; their revision 1 uses the shared
  `committed_at` as both `effective_at` and `recorded_at`, and they are immutable.
- Captures are immutable linked payments: `linked_payment_immutable` on correction.

## Historical holds

- `Authorization` gains `created_at` and `closed_at` (null while open; event time once closed).
- For `GET /me?as_of&known_at`, all four money fields describe the same view:
  `balance = total`, `available = total − held`.
- Hold timeline: starts at authorization creation; a nonfinal capture reduces the hold at capture
  time; final capture/void releases the remainder at that event time; clock expiry releases at
  `expires_at`. Non-expiry events are known at their server-assigned time; once creation is known,
  the expiry deadline is known. For queries beyond now, an open hold expires at its deadline.
- Model holds as an event list (create/capture/close) so `held(as_of, known_at)` can be derived.
  Seeded open holds are created at reset unless `created_at` is supplied; seeded closed holds need
  no prior-lifecycle reconstruction.
- `GET /statement` still contains money movements only: authorizations, releases and expiries are
  not payments; captures appear exactly once with their links. Old snapshots stay unchanged after
  any lifecycle action or correction.

## Export/import upgrade

- Keep `format_version: 1`; export now includes revisions, opening balances, hold events,
  `authorization.created_at`/`closed_at`, captures and correction idempotency records.
- Import must accept stage-1 and stage-2 exports: derive revision 1 + opening balances where
  absent, default missing authorizations to `[]`, accept stage-2 fields unchanged.
- Import remains atomic replace; invalid track/version/state → 422/400 without change; reset
  clears all. Tokens, password hashes, requests, payments, permissions and idempotency records
  still survive import. Snapshots are cleared on reset/import (they are session-scoped).

## Seat ownership

| Seat | Folder | Responsibility |
|---|---|---|
| dev (backend) | `src/`, `Dockerfile`, `RUN.md`, `requirements.txt` | ledger, revisions, corrections, statements, snapshots, historical holds, import/export |
| tester (backend) | `tests/` | extend stage-1+2 API regression with stage-3 tests |
| dev-fe / tester-fe / int-tester | — | no new work (UI unchanged) |

Backend checker verifies. Frontend/integration suites are regression-only; no new artifacts.

## Requirement → component map

| Spec area | Component |
|---|---|
| Payment timestamps / fixture | `store.py build_reset_state` |
| `/me?as_of&known_at` | `main.py`, `store.py` ledger |
| `/statement` + pagination + snapshot | `main.py`, `store.py` |
| Revisions & corrections | `models.py`, `store.py`, `main.py` |
| Effective/recorded time | ledger selection in `store.py` |
| Settlement/capture immutability | correction validation |
| Historical holds | hold event model in `store.py` |
| Export/import upgrade | `store.py build_import_state/export_state` |

## Edge cases to test (tester)

- Seeded `created_at` past/present/future; future → 422 and no change; loading seeded payments
  preserves seeded `balance`.
- `/me` without params unchanged (no `as_of` key); `as_of` in future = current; `as_of` before
  first payment = opening; payment exactly at `as_of` counts; naive date / bare date / empty → 422.
- Statement: oldest-first ties by payment id; opening+Σdelta=closing; `balance_after` pagination-
  independent; public third-party payments excluded; `from`/`to` half-open; `has_more`.
- Correction: sender-only 403; unknown 404; no token 401; required fields; stale revision 409;
  replay 200 after newer revisions; idempotency reuse 409; increase/decrease debits the right
  party; 0 amount reverses; `insufficient_funds` vs `historical_overdraft` precedence; sum
  invariant; failure is all-or-nothing; settlement member and capture → 422 linked_payment_immutable.
- `known_at`: selects latest recorded revision ≤ known_at; unknown payment contributes nothing;
  invalid/empty → 422; echo exactly.
- Snapshot: freeze/paging across later corrections; only limit/offset allowed with snapshot;
  unknown/foreign/pre-reset token 404; offset beyond end; final partial page `has_more`.
- Historical holds: `as_of`/`known_at` all four fields agree; nonfinal capture reduces held;
  final capture/void/expiry release remainder; expiry at `expires_at`; `closed_at` null while open;
  captures appear exactly once in statements; authorizations/releases/expiries not in statements.
- Export/import: accept stage-1 and stage-2 exports; revisions/opening/holds/captures survive
  import; snapshots cleared on reset/import; invalid state unchanged.

## Non-goals (do not build)

- New screens or frontend changes; corrections for settlement members or captures; reversal of
  request/split/settlement records beyond the defined correction endpoint; persistent storage;
  multi-currency; live streaming of statements.
