# Telegram bot (FE)

aiogram v3 bot — the product UI: deploy, monitor, and stop grids by chat.

Rule: never import the engine, adapters, or any exchange/chain SDK. The bot's only
backend line is the worker/gateway HTTP API (`X-User-Id` = Telegram user id) —
full contract in [`backend/README.md` §1](../../backend/README.md).

## Commands

```
/grid MARKET [BAND% [LEVELS [SIZE]]]   launch a grid (e.g. /grid BTCUSDT 0.8 12 0.002)
/status                                your grids: state, pnl, fills
/stop INSTANCE · /pause INSTANCE       close / halt a grid
/price MARKET                          live top-of-book
/balance                               venue equity
/health                                backend status
```

`/grid` sends the band as a fraction; the **worker** resolves lower/upper from the
user's live top-of-book (the bot never sees an exchange SDK). Ownership is
enforced server-side: stopping someone else's grid returns 409.

## Layout

| File | Role |
|---|---|
| `src/perpsbot/api.py` | `WorkerAPI` — thin typed client over the HTTP API |
| `src/perpsbot/commands.py` | all command logic — pure, aiogram-free, fully unit-tested |
| `src/perpsbot/main.py` | aiogram wiring + allowlist middleware (the only Telegram-aware file) |
| `src/perpsbot/config.py` | `PERPSBOT_*` settings + allowlist semantics |

## Run

```bash
cd frontend/telegram
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                                    # offline: commands, API client, allowlist

# dev backend (no keys, in-memory venue) in another shell:
cd backend && PYTHONPATH=src python3 -m perpsagent.app.worker     # :9000

cp .env.example .env                      # fill PERPSBOT_TOKEN from @BotFather
python -m perpsbot.main                   # long-polling
```

Settings (`.env`, prefix `PERPSBOT_`): `TOKEN` (BotFather), `API_URL` (worker for
dev, gateway for prod), `ALLOWLIST` (comma-separated user ids — empty allows
everyone, dev only), `DEFAULT_BAND/LEVELS/SIZE`.

TODO: per-user key onboarding (`/connect` → Fernet-encrypted credentials in
Postgres → real Bybit client per user), inline keyboards, fill notifications (SSE).
