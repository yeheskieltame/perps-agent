# Telegram bot (FE)

aiogram v3 bot, the product UI: connect your own Bybit keys, then deploy,
monitor, and stop grids by chat.

Rule: never import the engine, adapters, or any exchange/chain SDK. The bot's only
backend line is the worker/gateway HTTP API (`X-User-Id` = Telegram user id) -
full contract in [`backend/README.md` §1](../../backend/README.md).

## Commands

```
/connect                               link your Bybit API keys (DM only, guided)
/disconnect                            forget your keys, close your session
/grid MARKET [BAND% [LEVELS [SIZE]]]   launch a grid (e.g. /grid BTCUSDT 0.8 12 0.002)
/status                                your grids: state, pnl, fills
/stop INSTANCE · /pause INSTANCE       close / halt a grid
/price MARKET                          live top-of-book
/balance                               venue equity
/health                                backend status
/cancel                                abort the current dialog
```

## /connect, per-user key onboarding

A guided FSM dialog (key → secret → environment). Security posture:

- **DM only**, refused in groups; secrets never belong in shared chats.
- The bot **deletes each message** that carried a secret right after reading it
  (and tells you if Telegram refused the delete).
- Keys go straight to `PUT /v1/credentials`; the worker seals them with Fernet
  (`PERPSAGENT_CRED_MASTER_KEY`), at rest they exist only as ciphertext, and the
  API never echoes a key back (only `key_preview`, the first 4 chars).
- Environment is an **explicit choice**: reply `testnet` or `mainnet`, there is
  no default and no fuzzy matching, so real money can never come from a typo.
- Until a user connects, every trading command answers
  " Not connected, use /connect" (HTTP 401 from the worker).

Tell your users: create the API key with **Contract Trade only** (orders +
positions), **never enable Withdrawal**, no IP whitelist if they're behind a
rotating proxy/WARP.

## Layout

| File | Role |
|---|---|
| `src/perpsbot/api.py` | `WorkerAPI`, thin typed client over the HTTP API |
| `src/perpsbot/commands.py` | all command logic, pure, aiogram-free, fully unit-tested |
| `src/perpsbot/main.py` | aiogram wiring: allowlist middleware + the /connect FSM (the only Telegram-aware file) |
| `src/perpsbot/config.py` | `PERPSBOT_*` settings + allowlist semantics |

## Run

```bash
# backend worker with per-user credentials (one-time: generate the master key)
cd backend
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# put it in backend/.env as PERPSAGENT_CRED_MASTER_KEY, then:
PYTHONPATH=src python3 -m perpsagent.app.worker     # :9000

# the bot
cd frontend/telegram
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                                    # offline: commands, API client, allowlist
cp .env.example .env                      # fill PERPSBOT_TOKEN from @BotFather
python -m perpsbot.main                   # long-polling
```

Settings (`.env`, prefix `PERPSBOT_`): `TOKEN` (BotFather), `API_URL` (worker for
dev, gateway for prod), `ALLOWLIST` (comma-separated Telegram user ids, **set
this before inviting anyone**; empty allows everyone, dev only),
`DEFAULT_BAND/LEVELS/SIZE`.

Without `PERPSAGENT_CRED_MASTER_KEY` the worker serves a shared in-memory demo
venue (fine for trying the chat UX, useless for real trading).

TODO: inline keyboards, fill notifications (SSE).

---

Part of [Perps Agent](https://perpsagent.xyz). Full documentation: [docs.perpsagent.xyz](https://docs.perpsagent.xyz).
