# Deploy & Run

## Live on Mantle Sepolia (chainId 5003)

| Contract | Proxy |
|---|---|
| StrategyLedger | `0x128E925828952803E05157Ee4fEf54ac47cf1C88` |
| StrategyMemory | `0xC26E112437e5B6d739232732c665a64eb14Dc519` |
| Vault | `0x630370DC3a666c9b6816D5C9E8a3757242603eF3` |

Implementations source-verified; record in `contracts/deployments/mantle-sepolia.json`.
Explorer: https://sepolia.mantlescan.xyz

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
pytest                                          # 56 tests
python -m perpsagent.runner --mode dry          # full loop on fakes, no keys
```

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
