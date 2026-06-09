# Telegram bot (FE)

aiogram bot — the product UI: deploy, monitor, and stop grids by chat.

Rule: never import the engine, adapters, or any exchange/chain SDK. Talk to the
backend only through its published surfaces.

## API contract

Use the **worker/gateway HTTP API** (full docs + JSON shapes:
[`backend/README.md` §1](../../backend/README.md)):

- identity: `X-User-Id: <telegram chat id>` on every request
- `POST /v1/grids` · `DELETE /v1/grids/{id}` · `POST /v1/grids/{id}/pause`
- `GET /v1/status` · `GET /v1/balance`
- errors: `400` bad input · `409` not your grid

Dev backend (no keys, in-memory venue):

```bash
cd backend && PYTHONPATH=src python3 -m perpsagent.app.worker   # :9000
```

TODO: scaffold aiogram app + allowlist middleware; per-user key onboarding
(`/connect` → encrypted credentials in Postgres).
