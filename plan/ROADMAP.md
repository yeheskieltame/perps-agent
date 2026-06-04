# Perps Agent — Roadmap

Scope: **L1 + L2 + thin on-chain grid (Mantle DEX, spot)**. On-chain perp = later.
See [`../docs/CONCEPT.md`](../docs/CONCEPT.md) §8–9.

## Milestones (~4 weeks to Demo Day, 2–3 Jul 2026)
- **W1 — Spine.** Bybit adapter (testnet) + port grid engine; `StrategyLedger`
  (commit/attest) on Mantle testnet; backtest harness to seed `StrategyMemory`.
- **W2 — Brain (L1).** `StrategyMemory` + recall; agent loop
  (sense→recall→decide→commit→execute→attest); Elfa + Nansen clients; Telegram MVP.
- **W3 — Vault + on-chain grid + learning (L2).** `Vault` + x402 metering; thin
  iZiSwap grid adapter (1 market); contextual policy w/ experience replay; Verifier page.
- **W4 — Demo polish.** Ablation (baseline vs memory-on); end-to-end testnet run;
  deck, video, deployed contract addresses.

## Definition of done (hackathon)
- Vertical slice: chat → commit on-chain → grid trades on Bybit testnet → attest
  on-chain → Verifier page recomputes equity from chain.
- Ablation chart: higher winrate / lower drawdown with on-chain memory.
- Deployed contract addresses + open-source repo + >=2 min demo video.
