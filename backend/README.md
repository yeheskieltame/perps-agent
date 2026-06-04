# Perps Agent backend

Python trading engine + AI agent (the Verifiable Learning Loop). Hexagonal:
pure `domain/` core, ports, adapters behind a registry, `GridService` facade.

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,bybit]"
pytest
```

## Layout
- `domain/` — pure core: models · ports · grid · pnl · regime (no I/O)
- `adapters/exchanges/` — venues behind one `ExchangePort` (+ `registry.py`)
- `adapters/chain/` — Mantle contracts client (ledger · memory · vault)
- `adapters/signals/` — Elfa · Nansen
- `adapters/payments/` — x402 metering
- `agent/` — sense · recall · decide · learn (loop)
- `app/` — GridManager · GridService facade · safety

Add a venue: new folder under `adapters/exchanges/` implementing `ExchangePort`,
then one branch in `registry.py`. Engine + agent unchanged.

## Run the dry-run (no keys, no network)

The full Verifiable Learning Loop runs against the in-memory `FakeExchange` +
`MemoryChain`:

```bash
PYTHONPATH=src python3 scripts/demo_dry_run.py
PYTHONPATH=src python3 -m pytest tests/ -q     # 11 tests
```

## Implemented vs TODO

**Implemented & tested (40 pytest green):** pure grid math + regime keys, `FakeExchange`,
`MemoryChain`, `GridEngine` (grid + paired fills + FIFO realized PnL), `GridManager`,
`GridService` facade, the agent loop (sense→recall→decide→commit→execute→attest→learn),
a real **Bybit v5** adapter, the **MantleChainClient** (web3.py → deployed contracts;
encoding unit-tested), **Elfa + Nansen + Surf** signal clients fused into the regime (`agent/sense.py`;
Surf adds market microstructure — vol/funding/RSI), an explainable per-decision
rationale (`agent/reason.py`), a **SQLite store** with engine recovery
(persist instances + fills, replay realized PnL on restart), an **x402-gated
alpha API** (`app/alpha_api.py`: HTTP 402 → EIP-3009 USDC authorization →
verified-alpha response + settlement receipt), and a real
**iZiSwap (Mantle DEX)** adapter — on-chain limit-order grid with unit-tested
price<->point math + Mantle contract defaults (same engine, swap the venue).
`runner --mode dry` runs the full loop; `--venue mantle_dex` selects the on-chain grid.

**Next (per `plan/ROADMAP.md`):** live-verify iZiSwap `open_orders`/`stream_fills`
on Mantle testnet; on-chain x402 settlement broadcast (code ready, needs chain);
Telegram UI over `GridService`.
