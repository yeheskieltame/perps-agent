# Telegram bot (FE)

aiogram v3 bot, the product UI. It is a one-screen app: connect your own Bybit
keys, then launch, monitor, and stop grids by tapping buttons. Commands still work,
but nothing needs to be memorized.

Rule: never import the engine, adapters, or any exchange/chain SDK. The bot's only
backend line is the worker/gateway HTTP API (`X-User-Id` = Telegram user id), full
contract in [`backend/README.md`](../../backend/README.md).

## The product

- One-screen home (`/start`): Bybit equity, MNT wallet, open grids, and every
  action as a button.
- New Grid wizard: pick a coin, then a one-tap style (Safe, Balanced, Aggressive)
  sized from your balance, or "Set it myself" to tune every knob on one screen.
- Live positions, open orders, grid detail with PnL, and history, each with a
  one-tap close or stop.
- Shareable PnL cards: closing a position or grid, or tapping share on an open one,
  renders a branded PnL image.
- On-chain proofs in chat: every launch and close links to the commit and attest
  records on Mantle.

## Commands

Buttons cover everything; these are the typed equivalents.

```
/start                  the one-screen dashboard
/connect                link your Bybit API keys (DM only, guided)
/disconnect             forget your keys, close your session
/grid MARKET [k=v ...]  launch a grid (e.g. /grid BTCUSDT band=1 levels=10 lev=5)
/status                 your grids: state, pnl, fills
/positions  /orders     live Bybit positions and resting orders
/history                recently closed grids
/stop ID  /pause ID     close or halt a grid
/balance  /price MARKET venue equity and live top-of-book
/wallet  /topup         your managed MNT wallet and how to fund it
/help  /cancel          help, and abort the current dialog
```

## /connect, per-user key onboarding

A guided dialog (key, then secret, then environment). Security posture:

- DM only, refused in groups. Secrets never belong in shared chats.
- The bot deletes each message that carried a secret right after reading it.
- Keys go straight to `PUT /v1/credentials`. The worker seals them with Fernet
  (`PERPSAGENT_CRED_MASTER_KEY`); at rest they exist only as ciphertext, and the API
  never echoes a key back (only `key_preview`, the first 4 chars).
- Environment is an explicit choice: reply `testnet` or `mainnet`. There is no
  default and no fuzzy matching, so real money can never come from a typo.

Tell your users to create the API key with Contract Trade only, and never enable
Withdrawal.

## Layout

| File | Role |
|---|---|
| `src/perpsbot/api.py` | `WorkerAPI`, a thin typed client over the HTTP API |
| `src/perpsbot/commands.py` | command logic and render helpers, pure and aiogram-free, unit-tested |
| `src/perpsbot/keyboards.py` | inline keyboards and callback-data factories |
| `src/perpsbot/wizard.py` | the New Grid state machine |
| `src/perpsbot/pnlcard.py` | the shareable PnL card renderer |
| `src/perpsbot/main.py` | aiogram wiring: allowlist, handlers, FSM (the only Telegram-aware file) |
| `src/perpsbot/config.py` | `PERPSBOT_*` settings and allowlist |

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
pytest                                    # offline: commands, keyboards, API client
cp .env.example .env                      # fill PERPSBOT_TOKEN from @BotFather
python -m perpsbot.main                   # long-polling
```

Settings (`.env`, prefix `PERPSBOT_`): `TOKEN` (BotFather), `API_URL` (worker for
dev, gateway for prod), `ALLOWLIST` (comma-separated Telegram user ids; set this
before inviting anyone, empty allows everyone and is dev only).

Without `PERPSAGENT_CRED_MASTER_KEY` the worker serves a shared in-memory demo venue,
fine for trying the chat UX, useless for real trading.

---

Part of [Perps Agent](https://perpsagent.xyz). Full documentation: [docs.perpsagent.xyz](https://docs.perpsagent.xyz).
