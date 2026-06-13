#!/usr/bin/env bash
# Perps Agent — VPS bootstrap (Ubuntu/Debian, systemd + Postgres).
#
# Idempotent: safe to re-run. It installs OS deps, builds the two venvs, provisions
# a local Postgres role+db, seeds .env secrets it can generate (Fernet key, PG DSN),
# and installs the systemd units. It does NOT fill the Bybit/Mantle/bot secrets —
# you do that in the .env files, then enable the services (steps printed at the end).
#
#   sudo bash deploy/setup-vps.sh
#
# Overridable via env: APP_USER (default perps), PG_DB (perpsagent), PG_USER (perps).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${APP_USER:-perps}"
PG_DB="${PG_DB:-perpsagent}"
PG_USER="${PG_USER:-perps}"
BACKEND_DIR="$APP_DIR/backend"
BOT_DIR="$APP_DIR/frontend/telegram"
BACKEND_VENV="$BACKEND_DIR/.venv"
BOT_VENV="$BOT_DIR/.venv"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash deploy/setup-vps.sh)"

# ---- 1. OS packages ----------------------------------------------------------
log "Installing OS packages (python venv, postgres, git, openssl)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-dev build-essential \
    postgresql postgresql-contrib git openssl curl >/dev/null

# ---- 2. Python >= 3.11 -------------------------------------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  command -v "$c" >/dev/null 2>&1 || continue
  if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)'; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || die "need Python >= 3.11. On Ubuntu 22.04: add the deadsnakes PPA and install python3.11."
log "Using $($PY --version)"

# ---- 3. Service user ---------------------------------------------------------
if ! id "$APP_USER" >/dev/null 2>&1; then
  log "Creating system user '$APP_USER'…"
  useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi
log "Owning $APP_DIR → $APP_USER…"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

# ---- 4. Virtualenvs + installs (as the service user) -------------------------
build_venv() { # <dir> <venv> <pip-target>
  local dir="$1" venv="$2" target="$3"
  log "Building venv in $dir (pip install $target)…"
  sudo -u "$APP_USER" "$PY" -m venv "$venv"
  sudo -u "$APP_USER" "$venv/bin/pip" install --quiet --upgrade pip
  sudo -u "$APP_USER" bash -c "cd '$dir' && '$venv/bin/pip' install --quiet -e '$target'"
}
build_venv "$BACKEND_DIR" "$BACKEND_VENV" ".[bybit,postgres]"
build_venv "$BOT_DIR"     "$BOT_VENV"     "."

# ---- 5. Postgres role + database (idempotent) --------------------------------
log "Provisioning Postgres role '$PG_USER' + database '$PG_DB'…"
systemctl enable --now postgresql >/dev/null 2>&1 || true
PG_PASS="$(openssl rand -hex 16)"
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$PG_USER'" | grep -q 1; then
  sudo -u postgres psql -qc "ALTER ROLE \"$PG_USER\" WITH LOGIN PASSWORD '$PG_PASS';"
else
  sudo -u postgres psql -qc "CREATE ROLE \"$PG_USER\" WITH LOGIN PASSWORD '$PG_PASS';"
fi
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$PG_DB'" | grep -q 1 \
  || sudo -u postgres psql -qc "CREATE DATABASE \"$PG_DB\" OWNER \"$PG_USER\";"
PG_DSN="postgresql://$PG_USER:$PG_PASS@localhost:5432/$PG_DB"

# ---- 6. .env files + generated secrets ---------------------------------------
# ensure_env KEY VALUE FILE — set KEY only if it is absent or currently empty;
# never clobber a value you already filled in. Values may contain '/', ':', '@'.
ensure_env() {
  local key="$1" val="$2" file="$3"
  if grep -qE "^${key}=.+" "$file"; then return 0; fi          # already has a value → leave it
  if grep -qE "^${key}=" "$file"; then
    sudo -u "$APP_USER" python3 - "$file" "$key" "$val" <<'PY'
import sys, re
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
txt = open(path).read()
open(path, "w").write(re.sub(rf"(?m)^{re.escape(key)}=.*$", f"{key}={val}", txt))
PY
  else
    printf '%s=%s\n' "$key" "$val" | sudo -u "$APP_USER" tee -a "$file" >/dev/null
  fi
}

[ -f "$BACKEND_DIR/.env" ] || { log "Seeding backend/.env from example"; sudo -u "$APP_USER" cp "$BACKEND_DIR/.env.example" "$BACKEND_DIR/.env"; }
[ -f "$BOT_DIR/.env" ]     || { log "Seeding telegram/.env from example"; sudo -u "$APP_USER" cp "$BOT_DIR/.env.example" "$BOT_DIR/.env"; }

log "Writing Postgres DSN into backend/.env (always reconciled to the live password)…"
sudo -u "$APP_USER" python3 - "$BACKEND_DIR/.env" "$PG_DSN" <<'PY'
import sys, re
path, dsn = sys.argv[1], sys.argv[2]
txt = open(path).read()
txt = re.sub(r"(?m)^PERPSAGENT_POSTGRES_DSN=.*$", f"PERPSAGENT_POSTGRES_DSN={dsn}", txt)
open(path, "w").write(txt)
PY

if ! grep -qE '^PERPSAGENT_CRED_MASTER_KEY=.+' "$BACKEND_DIR/.env"; then
  log "Generating PERPSAGENT_CRED_MASTER_KEY (Fernet) — keep it; losing it bricks stored keys…"
  KEY="$("$BACKEND_VENV/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  ensure_env PERPSAGENT_CRED_MASTER_KEY "$KEY" "$BACKEND_DIR/.env"
fi
ensure_env PERPSAGENT_WORKER_HOST 127.0.0.1 "$BACKEND_DIR/.env"

# ---- 7. systemd units --------------------------------------------------------
log "Installing systemd units (substituting paths/user)…"
for unit in perpsagent-worker perpsbot perpsagent-alpha; do
  sed -e "s#/opt/perps-agent#${APP_DIR}#g" \
      -e "s#^User=perps#User=${APP_USER}#" \
      -e "s#^Group=perps#Group=${APP_USER}#" \
      "$APP_DIR/deploy/systemd/${unit}.service" > "/etc/systemd/system/${unit}.service"
done
systemctl daemon-reload

cat <<EOF

$(log "Bootstrap complete.")
Next — fill the secrets the script cannot generate, then start the services:

  1. backend/.env   → PERPSAGENT_BYBIT_* , PERPSAGENT_MANTLE_PRIVATE_KEY,
                      the 3 contract addresses, signal API keys.
                      (POSTGRES_DSN + CRED_MASTER_KEY are already set.)
  2. frontend/telegram/.env → PERPSBOT_TOKEN (from @BotFather),
                      PERPSBOT_ALLOWLIST (your Telegram user id, comma-separated).

  3. Enable + start:
       sudo systemctl enable --now perpsagent-worker perpsbot
       sudo systemctl status  perpsagent-worker perpsbot
       journalctl -u perpsagent-worker -f      # logs
       journalctl -u perpsbot -f

  4. (optional) x402 alpha API — sells verified StrategyMemory per call:
       # set PERPSAGENT_X402_* in backend/.env first, then:
       sudo systemctl enable --now perpsagent-alpha
       sudo ufw allow 8402/tcp                  # this one IS public (payment-gated)
       curl -i http://127.0.0.1:8402/v1/alpha/recall/BTCUSDT   # expect HTTP 402

  5. After editing any .env later:  sudo systemctl restart perpsagent-worker perpsbot

Firewall (recommended): the worker binds 127.0.0.1 only, but lock the box down anyway:
       sudo ufw allow OpenSSH && sudo ufw enable
EOF
