# Stage 2 integration test suite

End-to-end HTTP tests that cross the browser/UI surface and the JSON API of the
stage-2 service. They run against an **already-started** container and never
import its source code.

The service is server-rendered (Jinja2) with a JSON API behind the same routes:

* UI flow: HTML pages (`Accept: text/html`) authenticated by the HttpOnly
  session cookie set by the `/signup` and `/login` form handlers.
* API flow: JSON endpoints authenticated by `Authorization: Bearer <token>`.
* Shared routes: `/requests` and `/authorizations` serve HTML for
  `Accept: text/html` and JSON otherwise.

## What is covered

| File | Focus |
|---|---|
| `test_pages_auth.py` | Required routes, form signup/login cookie auth, every core `data-testid`, `auth-error` negatives, HTML/JSON content negotiation |
| `test_wallet_activity_ui.py` | `wallet-balance`/`available`/`held` vs `GET /me`, money formatting (incl. `minor_units: 0`), pay + request flows, available-based refusals, feed visibility and ordering |
| `test_authorizations_ui.py` | Authorize form, list rendering, `data-status`, capture/void buttons per state and party, empty and invisible states |
| `test_authorizations_flows.py` | Create/capture/void/expiry round trips, partial and non-final captures, remaining-amount prefill, validation and forbidden paths, wallet invariants |
| `test_upgrade_continuity.py` | Stage-1-shaped export → stage-2 import: cookie session and token survive, pending request stays payable, a lost-response payment is retryable with the same key/body |
| `test_concurrency_holds.py` | Concurrent payments/holds/captures preserve the total and keep `available >= 0`; replay/idempotency under load |

## Run

```sh
# 1. Build and start the stage-2 service (see ../RUN.md), e.g.
#    docker build -t pocketful-stage2 . && docker run --rm -p 8080:8080 -e PORT=8080 pocketful-stage2
# 2. Install test deps and run
pip install -r requirements.txt
POCKETFUL_BASE_URL=http://localhost:8080 pytest -q
```

Environment variables:

| Var | Default | Meaning |
|---|---|---|
| `POCKETFUL_BASE_URL` | `http://localhost:8080` | Service base URL |
| `POCKETFUL_READY_TIMEOUT` | `60` | Seconds to wait for `/health` |
| `POCKETFUL_HTTP_TIMEOUT` | `10` | Per-request timeout (seconds) |
