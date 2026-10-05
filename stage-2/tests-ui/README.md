# Stage 2 frontend (Playwright) test suite

Browser tests for the Pocketful stage-2 screens. They drive a real Chromium
against a running service and only rely on the public HTTP API and the
`data-testid` contract from `spec/stage-2.md`. They never import the
implementation.

## Run

```sh
# Playwright browser + Python driver (once)
pip install -r requirements.txt
python -m playwright install chromium

# Against an already-running service
POCKETFUL_BASE_URL=http://localhost:8080 pytest -q tests-ui

# Or let the suite start the service itself (uvicorn + src/main.py)
pytest -q tests-ui
```

Environment variables:

| Var | Default | Meaning |
|---|---|---|
| `POCKETFUL_BASE_URL` | (unset) | Service base URL. When unset the suite starts `src/main.py` with uvicorn on a free port. |
| `POCKETFUL_READY_TIMEOUT` | `60` | Seconds to wait for `/health` when starting the service. |
| `POCKETFUL_HTTP_TIMEOUT` | `10` | Per-request API timeout (seconds). |
| `POCKETFUL_UI_TIMEOUT` | `15000` | Per-locator timeout for the browser (ms). |
| `POCKETFUL_HEADLESS` | `1` | Set to `0` to run a headed browser. |

Every test resets the service (`POST /_test/reset`) before it runs, opens a
fresh browser context, and signs in through the real login screen, so tests are
independent, deterministic and order-insensitive.

## Layout

| File | Covers |
|---|---|
| `support.py` | HTTP client, fixture builders, `money()` formatting helper, UI helpers |
| `conftest.py` | Service/browser/page fixtures and the per-test reset |
| `test_auth_ui.py` | Signup, login, logout, `auth-error`, `current-user`, `current-handle` |
| `test_money_formatting.py` | `wallet-balance`/`wallet-available`/`wallet-held` exact text and `data-amount` (2, 0 and 3 decimals) |
| `test_pay_form.py` | Pay form decimal parsing, validation, retention, double-submit, field-change, refusals |
| `test_request_form.py` | Request form creation, decimal parsing, validation |
| `test_activity_feed.py` | Feed order, visibility, parties, amount, note, empty state |
| `test_requests_screen.py` | Incoming/outgoing lists, amounts, buttons per status/direction, empty state, stale pay |
| `test_split_screen.py` | Split preview matching the server rule, ordering, validation, formatting |
| `test_authorizations_ui.py` | `/authorizations` wallet numbers, authorize form, list, capture/void buttons, expiry, empty state |
| `test_competing_clients.py` | `wallet-refresh` latest-wins, uncertain retry, refusals against a changed wallet |
| `test_responsive_a11y.py` | 375 px viewport without horizontal scroll, labels, focus visibility |
