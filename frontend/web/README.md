# Web (FE)

Landing + public Verifier page: given a wallet/instance, recompute the results
straight from Mantle (StrategyLedger + StrategyMemory), independent of the
backend. Don't trust, verify.

## Data sources

1. **On-chain reads (Verifier)** — JSON-RPC view calls against Mantle Sepolia
   (`https://rpc.sepolia.mantle.xyz`, chainId 5003). Addresses, view functions,
   ABI location, and fixed-point scales: [`backend/README.md` §4](../../backend/README.md).
   - `StrategyLedger.getCommitment` / `getLatestAttestation` — proof the config
     hash predates the trades, and the attested outcome
   - `StrategyMemory.getByRegime` / `totalRecords` — the public experience buffer
2. **Alpha API (x402)** — optional paid widgets (live regime, verified recall):
   HTTP 402 flow documented in [`backend/README.md` §3](../../backend/README.md).

TODO: scaffold.
