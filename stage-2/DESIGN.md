# Pocketful — Stage 2 Design

## Context & baseline

- Spec: `/home/koda/Documents/dark-factory-ws4/dark-factory-wearedevs/pocketful/spec/stage-2.md`
- Stage-1 requirements continue to apply. Baseline: `band-work/result/stage-1/` (copied into
  `stage-2/src`, `Dockerfile`, `RUN.md`, `requirements.txt`).
- Stage folder: `./band-work/result/stage-2/`
- Stage-1 spec: `/home/koda/Documents/dark-factory-ws4/dark-factory-wearedevs/pocketful/spec/stage-1.md`

## Stack additions

- Keep FastAPI + uvicorn, single worker, in-memory state, one `asyncio.Lock` (same as stage 1).
- Add `jinja2` for server-side rendered HTML. `requirements.txt`: add `jinja2`.
- Serve templates from `templates/`, static assets from `static/` (`StaticFiles` mount at `/static`).
- Browser session: bearer token stored in an `HttpOnly`, `SameSite=Lax` cookie set by the HTML
  signup/login handlers; HTML pages authenticate from that cookie. JSON API auth is unchanged.

## Seat ownership & folders

| Seat | Folder(s) | Responsibility |
|---|---|---|
| dev (backend) | `src/`, `Dockerfile`, `RUN.md`, `requirements.txt` | API/domain changes, HTML route wiring, content negotiation, cookie auth |
| dev-fe (frontend) | `templates/`, `static/` | screens, styling, `data-testid`, client-side form/retry/preview logic, a11y/responsive |
| tester (backend) | `tests/` | extend stage-1 API suite with stage-2 API behavior |
| tester-fe (frontend) | `tests-ui/` | Playwright tests for screens, `data-testid`, formatting, form behavior |
| int-tester (integration) | `tests-integration/` | UI+API end-to-end flows, stage-1→stage-2 upgrade continuity, concurrency with holds |

Checkers (checker / checker-fe / int-checker) are tagged by the respective producer when ready.
Only backend + frontend + integration seats work on this stage; no other stage's spec may be read.

## Backend changes (dev)

- `models.py`: add `Authorization` (id, from/to user ids, amount, captured_amount, currency, note,
  visibility, status, expires_at, payment_id, payment_ids[], created_at); add `authorization_id`
  to `Payment`; add `authorization_ttl_seconds` to state; User keeps `balance` and gains derived
  `total`/`available`/`held` helpers (derived, not stored fields).
- `store.py`: authorization lifecycle (`create`, `capture`, `void`, `list`), lazy expiry
  (`effective_status`), `held`/`available` derivation, reset/import/export changes.
- `main.py`: new routes `POST /authorizations`, `POST /authorizations/{id}/capture`,
  `POST /authorizations/{id}/void`, `GET /authorizations`; `/me` adds `total`, `available`, `held`;
  evaluate `409 insufficient_funds` against `available`; content negotiation for `/requests` and
  `/authorizations`; HTML routes `/`, `/requests`, `/split`, `/signup`, `/login`, `/authorizations`;
  cookie-based browser auth.

## UI architecture

- Server-side rendering (Jinja2). `Accept: text/html` on `/requests` and `/authorizations` returns
  HTML; without it returns JSON. `/`, `/split`, `/signup`, `/login` are HTML-only.
- `/signup` and `/login` accept POST (form-encoded) → authenticate via store → set cookie → redirect
  to `/`; on failure re-render with `auth-error`.
- Money forms (pay, request, split, authorize, capture) submit via JS `fetch` to the JSON API using
  the cookie token, then update the DOM in place (no manual reload).
- `logout-button` clears the cookie and returns to `/login`.
- dev-fe owns all markup/styling/JS and every `data-testid` from the spec. dev owns the route
  handlers and must render the context below; dev and dev-fe must not edit the same file.

## Template context contract (dev ↔ dev-fe)

- Every signed-in page receives `current_user`: `user_id`, `display_name`, `handle`, `balance`,
  `total`, `available`, `held`, `currency`, `minor_units`.
- Global Jinja filter `money(minor)` → `"100.00 EUR"` / `"1200 JPY"` (exactly `minor_units`
  decimals, single space, no sign); dev implements it, dev-fe uses it everywhere.
- `index.html` receives `payments` (activity feed). `requests.html` receives `incoming`, `outgoing`.
  `authorizations.html` receives `incoming`, `outgoing`. `signup.html`/`login.html` receive optional
  `error`. `split.html` needs only globals (preview is computed client-side).
- `static/app.js` (dev-fe) owns: decimal→minor-units conversion, split preview (server §9 rule),
  form-value retention + idempotency-key lifecycle (reuse key until a field changes), `pay-uncertain`
  handling, and `wallet-refresh` latest-wins. Backend only guarantees §7 replay semantics.

## Authorizations & captures (precise semantics)

- `POST /authorizations` (payer = caller, idempotent): check `available >= amount`; create open hold
  with `expires_at = created_at + authorization_ttl_seconds`; `captured_amount=0`,
  `payment_id=null`, `payment_ids=[]`, `remaining_amount=amount`.
- Response always includes `remaining_amount` and `payment_ids`; `status` reflects effective status.
- `POST /authorizations/{id}/capture` (receiver = caller, idempotent, body `{amount?, final?}`):
  `amount` defaults to remaining; `final` defaults `true`. Captures move `amount` to receiver via an
  ordinary payment (`authorization_id` set, `request_id: null`, note/visibility copied). Final
  capture (or capturing the whole remainder) closes and releases any uncaptured remainder;
  `final:false` keeps a remainder `open`. `captured_amount` cumulative; `payment_id` = latest;
  `payment_ids` ordered.
- `POST /authorizations/{id}/void` (payer only, no idempotency key): releases the hold; `voided`
  once, repeat → 200; `captured`/`expired` → 409 `authorization_not_open`.
- Lazy expiry: an `open` authorization whose `expires_at <= now` behaves as `expired`, holds
  nothing, and releases its remainder. Applied on every read and write (no timer required).
- Capture error precedence: caller not receiver → 403; unknown → 404; effective-expired →
  409 `authorization_expired`; raw status not `open` → 409 `authorization_not_open`;
  `amount > remaining` → 422 `capture_exceeds_authorization`; `amount < 1`/non-integer → 422.

## Available funds rule

- `held` = Σ `remaining_amount` of effective-open holds where the caller is the payer.
- `available = total - held` (never negative; reset validation guarantees this).
- `POST /payments`, `POST /requests/{id}/pay` and settlement affordability now check `available`,
  not `total`. Settlement affordability: for each wallet `available + incoming - outgoing >= 0`.
  Captures spend reserved funds (reduce `total` and `held` together). Holds move no money.

## Fixture & reset changes

- `authorization_ttl_seconds`: default 600 when omitted; must be a positive integer when supplied.
- `authorizations` array optional (omitted = empty). Seeded entries: `id`, `from_user_id`,
  `to_user_id`, `amount`, `note`, `visibility`, `status` (open|captured|voided|expired),
  `expires_at`. Only effective `open` holds funds; `captured_amount` defaults 0.
- Reset error (422, change nothing): any negative seeded balance; or a user's seeded unexpired
  open holds sum > their `balance`.
- `available` is always derived, never seeded.

## Export/import upgrade compatibility

- Keep `format_version: 1`. Stage-2 export adds `authorization_ttl_seconds`, `authorizations`,
  and `authorization_id` on payments.
- Import must accept a stage-1 export unchanged: missing `authorizations` → `[]`; missing
  `authorization_ttl_seconds` → 600; payments without `authorization_id` → `null`. Tokens,
  password hashes, requests, payments, permissions and idempotency records still survive import
  (stage-1 rule), so a signed-in browser and a lost-response retry both survive the upgrade.
- Import stays atomic replace; invalid track/version/state → 422/400 without change; reset clears all.

## Idempotency (7 write paths)

- Stage-1 five plus `POST /authorizations` and `POST /authorizations/{id}/capture`.
- Same replay rules per user/key/(method,path,body). Capture replay body equality is JSON value
  equality (`{}` ≠ `{"amount": N}`). `void`, `decline`, `cancel` remain non-idempotent-key paths.

## Requirement → component map

| Spec area | Component |
|---|---|
| Screens & routes | `main.py` HTML routes, `templates/`, `static/` |
| Signup/login UI | `templates/signup.html`, `login.html`, cookie auth in `main.py` |
| Balance/pay/activity UI | `templates/index.html`, `static/app.js` |
| Requests/split UI | `templates/requests.html`, `split.html`, `static/app.js` |
| Authorizations UI | `templates/authorizations.html` |
| Authorizations/captures API | `store.py`, `models.py`, `main.py` |
| Available/holds invariants | `store.py` derivation + lock |
| Fixture/reset | `store.py build_reset_state` |
| Export/import upgrade | `store.py build_import_state/export_state` |
| Competing clients/uncertainty | `static/app.js` + §7 backend |

## Edge cases to test

- Backend (tester): hold/capture/void lifecycle incl. partial + `final:false`; cumulative captures;
  `capture_exceeds_authorization`; expiry laziness on `/me` and `/authorizations`; available-based
  409 on payments/request-pay/settlements; settlement with holds; seeded open hold past expiry;
  reset errors (negative balance, holds > balance); ttl default/validation; stage-1 export import;
  idempotent capture replay (`{}` vs explicit amount).
- Frontend (tester-fe): every `data-testid` and `data-status`/`data-visibility`/`data-amount`;
  `money()` formatting incl. `minor_units: 0`; pay/request/split decimal parsing and client-side
  rejection (`15.005`); double-submit moves money once; changing a field is a new payment;
  `wallet-refresh` latest-wins; `pay-uncertain` retry; buttons present only per state; 375px layout
  no horizontal scroll.
- Integration (int-tester): browser action → API state; API action → refresh; stage-1 service
  export → stage-2 import → browser still signed in → pending request payable → lost-response
  payment retryable with same key; concurrency with holds preserves `total` sum and
  `available >= 0`.

## Non-goals (do not build)

- Authorizing a request (request pay stays immediate); background polling/live sync; recovery
  across page reload; persistent storage; multi-currency; native mobile apps; separate frontend
  server or build step (single container SSR).

