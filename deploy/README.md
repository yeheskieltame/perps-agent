# VPS deploy — systemd + Postgres

Production layout for a single VPS (Ubuntu/Debian). Two long-running services:

| Service | What | Port |
|---|---|---|
| `perpsagent-worker` | engine — `GridService` HTTP API; owns sessions, fill streams, per-user encrypted Bybit clients | `127.0.0.1:9000` (loopback only) |
| `perpsbot` | Telegram bot (product UI) — long-polls Telegram, calls the worker over loopback | — (no inbound port) |

State + Fernet-encrypted Bybit keys live in **Postgres** (local). The bot uses
**long-polling**, so there is **no inbound webhook** — no domain, nginx, or TLS
required. The only public port you need open is SSH.

```
Telegram  ──polls──▶  perpsbot ──http://127.0.0.1:9000──▶  perpsagent-worker ──▶ Bybit / Mantle RPC
                                                                  │
                                                              Postgres (localhost:5432)
```

> **Security note.** The worker API is *unauthenticated* (it trusts an `X-User-Id`
> header). It binds `127.0.0.1` by default (`PERPSAGENT_WORKER_HOST`) so only the
> co-located bot can reach it. Do **not** set it to `0.0.0.0` on a public box.

---

## Quick start

```bash
# on the VPS, as a sudo user
sudo apt-get update && sudo apt-get install -y git
sudo git clone <repo-url> /opt/perps-agent      # or rsync your working copy here
cd /opt/perps-agent
sudo bash deploy/setup-vps.sh
```

`setup-vps.sh` is idempotent. It:

1. installs OS deps (python3 venv, postgresql, build tools);
2. picks a Python ≥ 3.11;
3. creates the `perps` system user and chowns the repo to it;
4. builds two venvs — `backend/.venv` (`-e .[bybit,postgres]`) and
   `frontend/telegram/.venv` (`-e .`);
5. provisions a local Postgres role + `perpsagent` database;
6. seeds `backend/.env` and `frontend/telegram/.env` from their `.env.example`,
   and fills the secrets it *can* generate — the **Postgres DSN** and a
   **Fernet `PERPSAGENT_CRED_MASTER_KEY`** — without overwriting anything you
   already set;
7. installs + `daemon-reload`s the two systemd units.

It deliberately does **not** invent the secrets only you hold.

### Then: fill the remaining secrets

`backend/.env`:
- `PERPSAGENT_BYBIT_API_KEY` / `PERPSAGENT_BYBIT_API_SECRET` — only if you run a
  single shared account; for the **multi-user product** each user connects their
  own keys via the bot's `/connect`, so you can leave these blank.
- `PERPSAGENT_MANTLE_PRIVATE_KEY` — signer for on-chain commit/attest (testnet).
- `PERPSAGENT_STRATEGY_LEDGER_ADDR` / `..._MEMORY_ADDR` / `PERPSAGENT_VAULT_ADDR`
  — the deployed proxies (Mantle Sepolia values are in the root `DEPLOY.md`).
- signal keys (`PERPSAGENT_ELFA_API_KEY`, `…_NANSEN_…`, `…_SURF_…`) if used.
- `PERPSAGENT_POSTGRES_DSN` and `PERPSAGENT_CRED_MASTER_KEY` are **already set** —
  leave them. **Back up the Fernet key**: losing it makes every stored user key
  unrecoverable.

`frontend/telegram/.env`:
- `PERPSBOT_TOKEN` — from @BotFather (required).
- `PERPSBOT_ALLOWLIST` — comma-separated Telegram user ids. **Set this in
  production** — empty means anyone can use the bot.
- `PERPSBOT_API_URL` — leave the default `http://127.0.0.1:9000`.

### Start

```bash
sudo systemctl enable --now perpsagent-worker perpsbot
sudo systemctl status perpsagent-worker perpsbot
journalctl -u perpsagent-worker -f      # follow worker logs
journalctl -u perpsbot -f               # follow bot logs
```

Health check (from the VPS): `curl http://127.0.0.1:9000/healthz` → `{"ok":true,...}`.

---

## Day-2 operations

```bash
# after editing any .env
sudo systemctl restart perpsagent-worker perpsbot

# deploy a new version
cd /opt/perps-agent && sudo -u perps git pull
sudo -u perps backend/.venv/bin/pip install -q -e 'backend[bybit,postgres]'   # if deps changed
sudo systemctl restart perpsagent-worker perpsbot

# firewall — only SSH needs to be open
sudo ufw allow OpenSSH && sudo ufw enable

# postgres backup
sudo -u postgres pg_dump perpsagent | gzip > perpsagent-$(date +%F).sql.gz
```

No schema migration step: `PostgresStore` runs `CREATE TABLE IF NOT EXISTS` on
first connect.

## Custom location / user

Clone elsewhere or want a different service user? Override and re-run:

```bash
sudo APP_USER=myuser bash deploy/setup-vps.sh
```

The script auto-detects the repo dir from its own path and rewrites the unit
files (`User=`, `Group=`, paths) to match before installing them.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| worker exits immediately | `assert_consistent()` env↔RPC mismatch — check `PERPSAGENT_ENV` vs `PERPSAGENT_MANTLE_RPC` in `backend/.env` (`journalctl -u perpsagent-worker`) |
| bot replies "Access denied" | your Telegram id isn't in `PERPSBOT_ALLOWLIST` |
| bot can't reach worker | worker not running, or `PERPSBOT_API_URL` ≠ `http://127.0.0.1:9000` |
| `/connect` says credential storage not configured | `PERPSAGENT_CRED_MASTER_KEY` empty — re-run the setup script |
| worker uses fake/demo prices | same — without the Fernet key the worker falls back to an in-memory `FakeExchange` |
