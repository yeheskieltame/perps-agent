# Perps Agent — Deploy & Run

## Verified in this build
- **Contracts:** `forge build` ✓, `forge test` 9/9 ✓, UUPS deploy on anvil ✓ with
  OpenZeppelin **upgrade-safety validations passing** (`@openzeppelin/upgrades-core`).
- **Backend:** 40 pytest ✓ (engine, agent loop, chain encoding, signals, iZiSwap math, SQLite recovery, x402 e2e);
  `runner --mode dry` runs the full Verifiable Learning Loop on the fakes.
- **Live web3 round-trip:** run locally via `backend/scripts/itest_chain.py`
  (commit→attest→write→recall against deployed contracts).


## LIVE on Mantle Sepolia (chainId 5003) — deployed & source-verified
| Contract | Proxy |
|---|---|
| StrategyLedger | `0x128E925828952803E05157Ee4fEf54ac47cf1C88` |
| StrategyMemory | `0xC26E112437e5B6d739232732c665a64eb14Dc519` |
| Vault | `0x630370DC3a666c9b6816D5C9E8a3757242603eF3` |

Implementations verified via Etherscan v2 (see `contracts/deployments/mantle-sepolia.json`).
Live integration proven: `scripts/itest_chain.py` → commit → attest → write → recall = **INTEGRATION OK**.
Explorer: https://sepolia.mantlescan.xyz

## 0. Prereqs
- Foundry: `curl -L https://foundry.paradigm.xyz | bash && foundryup`
- Node.js 18+ (OZ upgrade-safety validations use it)
- Python 3.11+

## 1. Contracts → Mantle testnet
```bash
cd contracts
make install        # forge install: forge-std, openzeppelin-foundry-upgrades, openzeppelin-contracts-upgradeable
make test           # forge clean && forge test  (9 tests)

export MANTLE_TESTNET_RPC=https://rpc.sepolia.mantle.xyz
export PRIVATE_KEY=0x...            # deployer → gets DEFAULT_ADMIN_ROLE + UPGRADER_ROLE
export PERPSAGENT_TREASURY=0x...      # vault fee sink (defaults to deployer)
forge clean
forge script script/Deploy.s.sol:Deploy \
  --rpc-url mantle_testnet --private-key $PRIVATE_KEY --broadcast --verify --force
# → record the 3 printed proxy addresses
```
Local quick deploy (no validations) for testing: `script/DeployLocal.s.sol` vs `anvil`.

## 2. Backend
```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,bybit]" web3
pytest                                              # 15 tests
PYTHONPATH=src python -m perpsagent.runner --mode dry # full loop on fakes, no keys
```

### Live mode
Copy `.env.example` → `.env`, fill Bybit keys + Mantle RPC/key + the 3 proxy
addresses, then:
```bash
PYTHONPATH=src python -m perpsagent.runner --mode live --market BTCUSDT
```

### Verify the chain client end-to-end (local)
```bash
anvil &                                             # terminal 1
cd contracts
forge script script/DeployLocal.s.sol:DeployLocal \
  --rpc-url http://127.0.0.1:8545 --private-key <anvil_key> --broadcast
# export PERPSAGENT_RPC, PERPSAGENT_PRIVATE_KEY, LEDGER, MEMORY, VAULT from its output
cd ../backend
PYTHONPATH=src python scripts/itest_chain.py        # prints "INTEGRATION OK"
```

## 3. Defaults & safety
- **Testnet always** unless `PERPSAGENT_ENV=mainnet`; the runner refuses env↔RPC mismatch.
- **Non-custodial:** Bybit keys are the user's; the Vault never bridges to the CEX.
- **Monetization:** builder fee per transaction (low %) + x402, settled on-chain.
- On-chain pre-commitment BEFORE trading; attest verified outcomes after.

## Still needs real credentials (cannot run in CI)
- Bybit testnet API key/secret · Mantle testnet RPC + funded deployer key.
- Elfa / Nansen API keys (signal clients — next milestone).
