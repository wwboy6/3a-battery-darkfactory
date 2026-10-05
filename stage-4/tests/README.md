# Stage 4 backend test suite

HTTP-level tests for the Pocketful stage-4 service. This is the stage-1..3
regression suite extended with refunds, the tightened correction rules and
batch corrections. The suite talks to an already-started container/service and
never imports its source code.

## Run

```sh
pip install -r requirements.txt
# build+start the service (see ../RUN.md), e.g.
#   docker build -t pocketful-stage4 .. && docker run --rm -p 8080:8080 -e PORT=8080 pocketful-stage4
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
- Stage-4 additions: `test_refunds.py`, `test_refund_correction_rules.py`,
  `test_correction_batches.py`, `test_export_import_stage4.py`,
  `test_concurrency_stage4.py`.

Each test resets the service with a canonical fixture, so tests are independent
and order-insensitive.
