# Stage 3 backend test suite

HTTP-level tests for the Pocketful stage-3 service. This is the stage-1+2
regression suite extended with the temporal ledger: payment timestamps,
historical balances (`GET /me?as_of&known_at`), statements with stable
pagination, revisions and payment corrections, historical holds and the
export/import upgrade. The suite talks to an already-started container/service
and never imports its source code.

## Run

```sh
pip install -r requirements.txt
# build+start the service (see ../RUN.md), e.g.
#   docker build -t pocketful-stage3 .. && docker run --rm -p 8080:8080 -e PORT=8080 pocketful-stage3
POCKETFUL_BASE_URL=http://localhost:8080 pytest -q .
```

Environment variables:

| Var | Default | Meaning |
|---|---|---|
| `POCKETFUL_BASE_URL` | `http://localhost:8080` | Service base URL |
| `POCKETFUL_READY_TIMEOUT` | `60` | Seconds to wait for `/health` |
| `POCKETFUL_HTTP_TIMEOUT` | `10` | Per-request timeout (seconds) |

## Layout

- Stage-1 regression: `test_auth.py`, `test_payments.py`, `test_requests.py`,
  `test_splits.py`, `test_activity.py`, `test_idempotency.py`,
  `test_export_import.py`, `test_settlements.py`, `test_concurrency.py`,
  `test_validation.py`, `test_health_and_conventions.py`,
  `test_reset_and_fixture.py`, `test_me.py`.
- Stage-2 additions: `test_authorization_lifecycle.py`,
  `test_authorization_expiry.py`, `test_authorization_idempotency.py`,
  `test_authorizations_list.py`, `test_available_funds.py`,
  `test_reset_authorizations.py`, `test_export_import_upgrade.py`,
  `test_me_holds.py`, `test_concurrency_holds.py`.
- Stage-3 additions: `test_payment_timestamps.py`, `test_me_as_of.py`,
  `test_me_known_at.py`, `test_statement.py`, `test_statement_snapshot.py`,
  `test_revisions.py`, `test_payment_corrections.py`,
  `test_historical_holds.py`, `test_export_import_stage3.py`,
  `test_concurrency_stage3.py`.

Each test resets the service with a canonical fixture, so tests are independent
and order-insensitive.
