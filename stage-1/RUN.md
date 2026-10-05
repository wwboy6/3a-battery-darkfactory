# Running the Stage 1 service

Build and start the container (no manual setup required):

```sh
docker build -t pocketful-stage1 .
docker run --rm -e PORT=8080 -p 8080:8080 pocketful-stage1
```

The service listens on `0.0.0.0:${PORT}` (default `8080`).

## Smoke check

```sh
curl -s http://localhost:8080/health
# {"status":"ok"}
```

## Optional: run without Docker

```sh
pip install -r requirements.txt
cd src
PORT=8080 uvicorn main:app --host 0.0.0.0 --port "${PORT:-8080}" --workers 1
```

All state is in-memory; `POST /_test/reset` installs a fixture and clears prior state.
