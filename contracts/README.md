# Perps Agent contracts (Mantle)

Foundry project. The on-chain **brain + trust layer** (docs/CONCEPT.md §3-4).
All three are **UUPS-upgradeable** (OpenZeppelin Contracts v5), use **ERC-7201
namespaced storage**, **custom errors**, **AccessControl**, and — for the Vault —
**ReentrancyGuard + Pausable + SafeERC20**.

| Contract | Layer | Role |
|----------|:----:|------|
| `StrategyLedger` | L1 | Pre-commit config hash BEFORE trading; attest verified outcomes after |
| `StrategyMemory` | L1 | Append-only public experience buffer: regime → params → outcome; read back by the agent (recall) |
| `Vault` | L2 | Performance bond + on-chain fee settlement (mETH / USDe / USDC) |

Solidity **0.8.34**. Roles: `DEFAULT_ADMIN_ROLE`, `UPGRADER_ROLE` (gates
`_authorizeUpgrade`), plus `PAUSER_ROLE` / `FEE_MANAGER_ROLE` on the Vault.

## Install

```bash
forge install foundry-rs/forge-std
forge install OpenZeppelin/openzeppelin-foundry-upgrades
forge install OpenZeppelin/openzeppelin-contracts-upgradeable
```

`remappings.txt` is already configured per the OpenZeppelin Foundry Upgrades docs.
The upgrades plugin runs storage-layout safety checks, which need **Node.js** and
`ffi`/`ast`/`build_info`/`storageLayout` (already set in `foundry.toml`).

## Build & test

```bash
forge clean && forge build      # clean is required by the upgrades plugin
forge test -vvv
```

Tests use `UnsafeUpgrades` (no ffi) so they run anywhere; deployment uses the
validated `Upgrades` library.

## Deploy (testnet by default)

```bash
export MANTLE_TESTNET_RPC=https://rpc.sepolia.mantle.xyz
export PERPSAGENT_ADMIN=0xYourAdmin           # gets admin + upgrader roles
export PERPSAGENT_TREASURY=0xYourTreasury      # vault fee sink
forge clean
forge script script/Deploy.s.sol:Deploy --rpc-url mantle_testnet --broadcast --verify --force
```

## Upgrading

Add `@custom:oz-upgrades-from <OldContract>` to the new version (or pass
`referenceContract`), then call `Upgrades.upgradeProxy(proxy, "NewContract.sol", "")`
with `--sender <UPGRADER>`. The plugin validates storage-layout compatibility.

## Security notes

- Pre-commitment: only the committing agent may `attest` its instance.
- `StrategyMemory.write` stamps `agent = msg.sender` and `timestamp = block` to
  prevent spoofed records.
- Vault follows Checks-Effects-Interactions, is `nonReentrant`, credits the
  *received* amount (fee-on-transfer safe), and keeps `withdraw` available even
  when paused so an emergency pause can never trap funds.
- `_authorizeUpgrade` is gated by `UPGRADER_ROLE`.
- Optimization target on-chain is the signed `riskAdjBps` (risk-adjusted), never
  raw PnL — "better systems, not the highest PnL".

> Not audited. Testnet only until reviewed.
