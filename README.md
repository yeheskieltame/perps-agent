# Perps Agent

**X / Twitter: [@perpsagent](https://x.com/perpsagent)**

A verifiable, self-improving **grid-trading agent**. It **executes on Bybit**, and
**thinks, learns, and proves on Mantle**.

- Concept + judging strategy: [`docs/CONCEPT.md`](docs/CONCEPT.md)
- Plan / roadmap: [`plan/ROADMAP.md`](plan/ROADMAP.md)
- Built for the Mantle **Turing Test Hackathon 2026 — AI Awakening**, track
  **AI · Trading & Strategy (BGA)**.

## Monorepo
| Path | What |
|------|------|
| `contracts/` | Mantle Solidity (Foundry): StrategyLedger · StrategyMemory · Vault |
| `backend/` | Python engine + AI agent (the Verifiable Learning Loop) |
| `frontend/telegram/` | aiogram bot — product UI |
| `frontend/web/` | public Verifier page (recompute equity from chain) |
| `docs/` · `plan/` | concept, architecture, roadmap |

> Concept/logic adapted from the `deltaperps` grid-bot platform.
> Defaults: **testnet always** unless explicitly told mainnet.
