# Pocketful — Stage 4 Design

## Context & baseline

- Spec: `/home/koda/Documents/dark-factory-ws4/dark-factory-wearedevs/pocketful/spec/stage-4.md`
- Stages 1–3 continue to apply. Baseline: `band-work/result/stage-3/` (copied into `stage-4/`).
- Stage folder: `./band-work/result/stage-4/`
- Earlier specs:
  - `/home/koda/Documents/dark-factory-ws4/dark-factory-wearedevs/pocketful/spec/stage-1.md`
  - `/home/koda/Documents/dark-factory-ws4/dark-factory-wearedevs/pocketful/spec/stage-2.md`
  - `/home/koda/Documents/dark-factory-ws4/dark-factory-wearedevs/pocketful/spec/stage-3.md`
- **Backend-only stage.** No new screens or `data-testid`; stage-2 UI stays unchanged.

## Stack

- Unchanged: FastAPI + uvicorn (single worker), in-memory state, one `asyncio.Lock`, Jinja2 SSR.
- No new dependencies. `format_version` stays `1`.

## Refunds

- `POST /payments/{payment_id}/refunds` (idempotent write path #9). Only the original receiver
  (403 `forbidden` otherwise; 401 no token; 404 unknown payment).
- Body `{"amount": 200}`: positive integer, required; invalid → 422 `validation_failed`.
- Target may be a direct payment, request payment, or capture; never a refund
  (refund of a refund → 422 `invalid_refund_target`).
- Cumulative refunds on a payment must not exceed its **current corrected amount** →
  422 `refund_exceeds_payment`.
- A refund is a new ordinary payment in the opposite direction with `refund_of` = target
  `payment_id`, `request_id: null`, `authorization_id: null`, and the target's note/visibility.
  Returns 201 with that payment; replay returns 200 with the original body.
- Moves money from the receiver's `available` funds atomically; insufficient → 409
  `insufficient_funds`. Never reopens a request/authorization or restores a released hold.
- All other payments carry `refund_of: null`.

## Corrections (stage-3 behaviour tightened)

- Captures **and refund payments** cannot be corrected → 422 `linked_payment_immutable`
  (detected via `authorization_id != null` or `refund_of != null`).
- A correction may not reduce a payment below its already-refunded amount → 422
  `refund_exceeds_payment` (new amount ≥ cumulative refunds).
- Correction debits are checked against the debited party's current `available` funds
  (already the stage-3 immediate-debit rule).

## Batch corrections

- `POST /correction-batches` (idempotent write path #10). Settlement operator required, same
  401/403 rules as `POST /settlements`.
- Body `{"corrections":[...]}`: 1..32 items with **distinct** `payment_id`, else 422.
  Each item has the ordinary correction fields and validation; unknown payment → 404; stale
  `expected_revision` → 409 `stale_revision`.
- Operator may correct ordinary, request and settlement payments; captures and refunds remain
  immutable. Correcting any settlement member requires **every** member of that settlement
  (else 422 `incomplete_settlement`). Members of one settlement must share identical effective
  instants (offset spellings may differ) → 422 `validation_failed`.
- Error precedence: item errors in input order → settlement completeness → combined current
  `available` funds → historical `total`/`available` at every effective/event boundary.
  Codes: `linked_payment_immutable`, `refund_exceeds_payment`, `insufficient_funds`,
  `historical_overdraft`. A rejected batch changes nothing (history, balances, idempotency).
- Returns 201 with `correction_batch_id`, `recorded_at`, and `revisions` in input order. All new
  revisions share `recorded_at`, strictly later than the previous recorded_at of every member;
  each revision also carries `correction_batch_id`. Effective times must not be later than now.
- Original payments, receipts and settlement retries never change. Statements reflect the new
  revisions; earlier snapshot tokens keep paging their frozen entries. Replay → 200 original body.

## Model changes

- `Payment`: add `refund_of` (string id or null).
- `Revision`: add `correction_batch_id` (string id or null for single corrections).
- New `CorrectionBatch` record: `id`, `recorded_at`, member revision ids in input order.
- Statement snapshots become part of exported/imported state (see below).

## Export/import upgrade

- Stage-4 export includes refunds (`refund_of`), correction batches, revision
  `correction_batch_id`, settlement membership, and statement snapshots.
- Import must accept stage-1, stage-2 and stage-3 exports (project missing fields to defaults:
  `refund_of: null`, no batches, no snapshots where absent) and retain settlement membership,
  corrections and snapshots where present.
- Import remains atomic replace; invalid track/version/state → 422/400 without change; reset
  clears all. Stage-1/2/3 data (tokens, passwords, requests, payments, permissions, holds,
  idempotency) still survives.

## Seat ownership

| Seat | Folder | Responsibility |
|---|---|---|
| dev (backend) | `src/`, `Dockerfile`, `RUN.md`, `requirements.txt` | refunds, correction tweaks, correction batches, export/import |
| tester (backend) | `tests/` | extend stage-1..3 regression with stage-4 tests |
| dev-fe / tester-fe / int-tester | — | no new work (UI unchanged) |

Backend checker verifies. Frontend/integration suites are regression-only; no new artifacts.

## Requirement → component map

| Spec area | Component |
|---|---|
| Refunds | `store.py`, `main.py` |
| Correction immutability/refund floor | `store.py` correction validation |
| Correction batches | `store.py`, `models.py`, `main.py` |
| Settlement membership rules | batch validation |
| Export/import incl. snapshots | `store.py build_import_state/export_state` |

## Edge cases to test (tester)

- Refund: receiver-only 403/401/404; unknown target 404; amount required and invalid → 422;
  refund of refund → 422 `invalid_refund_target`; cumulative refunds equal corrected amount
  allowed; exceeding it → 422 `refund_exceeds_payment`; insufficient receiver available → 409;
  replay → 200 original; refund appears as opposite-direction payment with correct
  `refund_of`/nulls and copied note/visibility; capture target refundable.
- Corrections: capture → 422 `linked_payment_immutable`; refund payment → 422
  `linked_payment_immutable`; reducing below already-refunded amount → 422
  `refund_exceeds_payment`; correction debit checked against `available`.
- Batch: operator auth (401 no token / 403 non-operator); 0/33 items or duplicate payment_id →
  422; item error precedence in input order; settlement member requires all members
  (422 incomplete_settlement); mixed effective instants in a settlement → 422; combined
  affordability and historical overdraft across the batch; all-or-nothing rollback; shared
  `recorded_at` strictly later than every member's prior; `correction_batch_id` on revisions;
  replay → 200 original; statements update while old snapshots stay frozen.
- Concurrency: concurrent corrections sharing an expected payment revision → one winner;
  concurrent refunds never exceed corrected amount; totals/available invariants preserved.
- Export/import: accept stage-1/2/3 exports; refunds, batches, settlement membership and
  snapshots survive import; invalid import unchanged; reset clears all.

## Non-goals (do not build)

- New screens or frontend changes; refunds of refunds; corrections of captures or refunds;
  reopening requests/authorizations or restoring released holds on refund; persistent storage;
  multi-currency.
