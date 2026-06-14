# Deploy & Run

## Live on Mantle Sepolia (chainId 5003)

| Contract | Proxy |
|---|---|
| StrategyLedger | `0x128E925828952803E05157Ee4fEf54ac47cf1C88` |
| StrategyMemory | `0xC26E112437e5B6d739232732c665a64eb14Dc519` |
| Vault | `0x630370DC3a666c9b6816D5C9E8a3757242603eF3` |

Implementations source-verified; record in `contracts/deployments/mantle-sepolia.json`.
Explorer: https://sepolia.mantlescan.xyz

## Host the product on a VPS

Worker + Telegram bot as two systemd services, state in local Postgres, bot on
long-polling (no domain/TLS needed). One command on the box:
`sudo bash deploy/setup-vps.sh`. Full guide: [`deploy/README.md`](deploy/README.md).

## Prereqs

- Foundry (`curl -L https://foundry.paradigm.xyz | bash && foundryup`)
- Node.js 18+ (OZ upgrade-safety validations)
- Python 3.11+

## Contracts → Mantle testnet

```bash
cd contracts
make install
forge clean && forge test
export MANTLE_TESTNET_RPC=https://rpc.sepolia.mantle.xyz
export PRIVATE_KEY=0x...            # deployer: admin + upgrader
export PERPSAGENT_TREASURY=0x...    # vault fee sink (defaults to deployer)
forge script script/Deploy.s.sol:Deploy \
  --rpc-url mantle_testnet --private-key $PRIVATE_KEY --broadcast --verify --force
```

## Backend

```bash
cd backend
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,bybit]"
pytest                                          # full suite (+PG tests when a DSN is set)
python -m perpsagent.runner --mode dry          # full loop on fakes, no keys
```

### Durable store (Postgres via Docker)

The multi-tenant store + encrypted credentials run on Postgres (`.[postgres]`
extra). Spin a local one up and point the app/tests at it:

```bash
cd backend
pip install -e ".[postgres]"
docker compose up -d --wait                     # local PG on :55432
DSN=postgresql://perps:perps@localhost:55432/perpsagent_test
PERPSAGENT_TEST_PG_DSN=$DSN pytest tests/test_postgres_store.py   # PG integration tests
PERPSAGENT_POSTGRES_DSN=$DSN python -m perpsagent.runner --mode live ...   # run on PG
docker compose down                             # stop (-v also wipes data)
```

Without a DSN, the store falls back to SQLite (`PERPSAGENT_STORE_DB_PATH`) and the
PG integration tests skip. Set `PERPSAGENT_CRED_MASTER_KEY` (a Fernet key) to seal
per-user venue keys — never commit/log it.

### Sharded deploy (workers + gateway)

Horizontal scale = N worker processes (one shard each) behind one stateless gateway
that routes by `user_id` (consistent hashing — `plan/SCALING.md` #10). Each worker
owns its users' sessions/streams/clients; the gateway holds no state.

```bash
# one worker per shard (distinct node id + port), durable store + creds shared
PERPSAGENT_SHARD_COUNT=2 PERPSAGENT_SHARD_NODE=0 PERPSAGENT_WORKER_PORT=9000 \
  PERPSAGENT_POSTGRES_DSN=$DSN PERPSAGENT_CRED_MASTER_KEY=$KEY \
  python -m perpsagent.app.worker
PERPSAGENT_SHARD_COUNT=2 PERPSAGENT_SHARD_NODE=1 PERPSAGENT_WORKER_PORT=9001 ... python -m perpsagent.app.worker

# gateway in front (route map must match the shard ids)
PERPSAGENT_SHARD_COUNT=2 PERPSAGENT_GATEWAY_PORT=8080 \
  PERPSAGENT_SHARD_URLS='{"0":"http://127.0.0.1:9000","1":"http://127.0.0.1:9001"}' \
  python -m perpsagent.app.gateway
```

Clients call the gateway with an `X-User-Id` header; it forwards to the owning
worker. The gateway is unauthenticated by design (terminate auth at your ingress),
and grids run through the `GridService` facade — the verifiable LearningLoop is wired
per user on launch/stop.

Live: copy `.env.example` → `.env`, fill Bybit testnet keys + Mantle RPC/key + the
3 proxy addresses + signal keys, then:

```bash
python -m perpsagent.runner --mode live --venue bybit --market BTCUSDT \
  --leverage 1 --band 0.012 --order-size 0.005 \
  --recenter-interval 20 --max-inventory 0.05 --trail 0.3 --trail-arm 10
```

Preflight every live connection first: `python scripts/preflight.py` (PASS/FAIL
matrix for Bybit, Mantle, signals). Verify the chain round-trip locally with
`scripts/itest_chain.py` (commit → attest → write → recall) against a local anvil
deploy.

## Defaults

- Testnet always unless `PERPSAGENT_ENV=mainnet`; runner refuses env↔RPC mismatch.
- Non-custodial: Bybit keys are the user's; the Vault never bridges to the CEX.
- Commit on-chain before trading; attest the verified outcome after.
