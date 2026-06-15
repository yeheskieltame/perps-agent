# Contracts (Mantle)

Foundry project, the on-chain brain + trust layer. Solidity 0.8.34, all three
UUPS-upgradeable (OpenZeppelin v5), ERC-7201 namespaced storage, custom errors,
AccessControl; Vault adds ReentrancyGuard + Pausable + SafeERC20.

| Contract | Layer | Role |
|----------|:----:|------|
| `StrategyLedger` | L1 | Commit config hash before trading; attest verified outcome after |
| `StrategyMemory` | L1 | Append-only `regime → params → outcome`; read back by the agent (recall) |
| `Vault` | L2 | Performance bond + on-chain fee settlement (mETH / USDe / USDC) |

Roles: `DEFAULT_ADMIN_ROLE`, `UPGRADER_ROLE` (gates `_authorizeUpgrade`), plus
`PAUSER_ROLE` / `FEE_MANAGER_ROLE` on the Vault.

## Build, test, deploy

```bash
make install                     # forge-std + OZ upgrades + OZ contracts-upgradeable
forge clean && forge build       # clean is required by the upgrades plugin
forge test -vvv

export MANTLE_TESTNET_RPC=https://rpc.sepolia.mantle.xyz
export PERPSAGENT_ADMIN=0x...     # admin + upgrader
export PERPSAGENT_TREASURY=0x...  # vault fee sink
forge script script/Deploy.s.sol:Deploy --rpc-url mantle_testnet --broadcast --verify --force
```

Tests use `UnsafeUpgrades` (no ffi, run anywhere); deploy uses the validated
`Upgrades` library (storage-layout safety checks, needs Node.js). Local quick
deploy: `script/DeployLocal.s.sol` against `anvil`.

## Security

- Only the committing agent may `attest` its instance; `StrategyMemory.write`
  stamps `agent = msg.sender` + block timestamp (no spoofed records).
- Vault is Checks-Effects-Interactions + `nonReentrant`, credits the *received*
  amount (fee-on-transfer safe), and keeps `withdraw` available while paused.
- On-chain objective is signed `riskAdjBps` (risk-adjusted), never raw PnL.

Not audited. Testnet only until reviewed.

---

Part of [Perps Agent](https://perpsagent.xyz). Full documentation: [docs.perpsagent.xyz](https://docs.perpsagent.xyz).
