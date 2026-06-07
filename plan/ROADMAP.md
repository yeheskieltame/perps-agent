# Status

Scope: L1 + L2 + on-chain spot grid (Mantle DEX). On-chain perp is later.

## Shipped

- Contracts live on Mantle Sepolia (Ledger, Memory, Vault); commit/attest/recall
  round-trip verified.
- Agent loop end-to-end: sense → recall → decide → commit → execute → attest → learn.
- Adaptive grid engine: dynamic re-center (inventory-aware), circuit breaker,
  take-profit / trailing-stop, signed-position accounting, tunable params.
- Adapters: Bybit v5 (execution), iZiSwap (on-chain grid), Elfa/Nansen/Surf
  (signals), x402 metering, SQLite recovery. 56 backend tests.

## Next

- Frontend scaffolds (Telegram bot, web Verifier) over `GridService`.
- Equity-curve tracking for MAE / Sortino (currently `max_adverse_excursion = 0`).
- Implement `safety` alerting; mainnet only after soak.
