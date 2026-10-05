# Stage 1 backend test suite

HTTP-level tests for the Pocketful stage-1 service. They run against a
already-started container/service and never import its source code.

## Run

```sh
# 1. Build and start the service (see ../RUN.md), e.g.
#    docker build -t pocketful . && docker run --rm -p 8080:8080 -e PORT=8080 pocketful
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

Each test resets the service with a canonical fixture, so tests are independent
and order-insensitive.
