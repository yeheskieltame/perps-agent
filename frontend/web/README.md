# Web (FE)

The landing page and public Verifier. Live at [perpsagent.xyz](https://perpsagent.xyz),
with full documentation at [docs.perpsagent.xyz](https://docs.perpsagent.xyz).

The Verifier recomputes a result straight from Mantle (StrategyLedger and
StrategyMemory), independent of the backend. Don't trust, verify.

## Data sources

1. On-chain reads (Verifier): JSON-RPC view calls against Mantle Sepolia
   (`https://rpc.sepolia.mantle.xyz`, chainId 5003). Addresses, view functions and
   fixed-point scales are in [`backend/README.md`](../../backend/README.md).
   - `StrategyLedger.getCommitment` and `getLatestAttestation`: proof the config hash
     predates the trades, and the attested outcome.
   - `StrategyMemory.getByRegime` and `totalRecords`: the public experience buffer.
2. Alpha API (x402): optional paid widgets (live regime, verified recall) over the
   HTTP 402 flow documented in [`backend/README.md`](../../backend/README.md).

---

Part of [Perps Agent](https://perpsagent.xyz). Full documentation: [docs.perpsagent.xyz](https://docs.perpsagent.xyz).
