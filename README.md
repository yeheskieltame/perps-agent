# Perps Agent

A verifiable, self-improving grid-trading agent. It executes on Bybit, and commits,
learns, and proves on Mantle. X: [@perpsagent](https://x.com/perpsagent).

Built for the Mantle Turing Test Hackathon 2026 — AI · Trading & Strategy (BGA) track.

## Architecture

```mermaid
flowchart TB
  subgraph FE["Frontend (talks to GridService only)"]
    TG[Telegram bot]
    WEB[Web Verifier]
  end
  subgraph BE["Backend — Python, hexagonal"]
    SVC[GridService facade]
    AGENT["Agent loop: sense, recall, decide, learn"]
    ENG["Grid engine: re-center, breaker, take-profit"]
    REG[Adapter registry]
    SVC --> AGENT --> ENG --> REG
  end
  subgraph SIG[Signals]
    ELFA[Elfa — social]
    NANSEN[Nansen — smart money]
    SURF[Surf — microstructure]
  end
  subgraph CHAIN["Mantle — on-chain brain"]
    LEDGER[StrategyLedger — commit + attest]
    MEM[StrategyMemory — recall]
    VAULT[Vault — bond + fees]
  end
  FE --> SVC
  AGENT --> SIG
  AGENT --> CHAIN
  REG --> BYBIT[Bybit v5 — execution]
  REG --> DEX[iZiSwap — on-chain grid]
```

## Monorepo

| Path | Role |
|------|------|
| `contracts/` | Mantle Solidity (Foundry): StrategyLedger, StrategyMemory, Vault |
| `backend/` | Python engine + agent + the `GridService` facade |
| `frontend/telegram/` | aiogram bot — product UI |
| `frontend/web/` | public Verifier page — recompute equity from chain |
| `docs/`, `plan/` | concept/strategy, status |

The one cross-team rule: the UI never imports the engine, adapters, or any
exchange/chain SDK. It talks to the typed `GridService` facade only.

## Quickstart

```bash
cd backend
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,bybit]"
pytest                                              # 56 tests
python -m perpsagent.runner --mode dry              # full loop on fakes, no keys

# live (testnet): fill backend/.env, then
python -m perpsagent.runner --mode live --venue bybit --market BTCUSDT \
  --leverage 1 --band 0.012 --order-size 0.005 \
  --recenter-interval 20 --max-inventory 0.05 --trail 0.3 --trail-arm 10
```

- Strategy + the learning loop: [`docs/CONCEPT.md`](docs/CONCEPT.md)
- Backend, `GridService` API, runner flags: [`backend/README.md`](backend/README.md)
- Contracts: [`contracts/README.md`](contracts/README.md) · Deploy/run: [`DEPLOY.md`](DEPLOY.md)

Defaults: testnet only unless `PERPSAGENT_ENV=mainnet`; the runner refuses an
env↔RPC mismatch. Grid logic adapted from the `deltaperps` platform.
