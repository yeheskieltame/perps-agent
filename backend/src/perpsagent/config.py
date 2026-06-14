"""Settings. Testnet by default; refuse to start on env<->URL mismatch
(mirrors deltaperps). Never hardcode keys — load from .env."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PERPSAGENT_", env_file=".env", extra="ignore")

    env: str = "testnet"  # testnet | mainnet
    # Trade real money on Bybit (env=mainnet) while keeping on-chain proofs on a
    # free testnet chain. Explicit opt-in so the mainnet label never *silently*
    # implies testnet proofs. Only valid when env=mainnet.
    proofs_on_testnet: bool = False

    # Bybit (user-supplied keys; never commit)
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = True
    bybit_rate_limit: float = 10.0  # client-side req/s cap per account (0 disables)
    bybit_max_retries: int = 3      # retries on transient Bybit/HTTP errors

    # Risk / execution — user choices, enforced on the venue (runner flags override)
    leverage: str = "1"                # user-chosen leverage (e.g. "10"); set_leverage'd on launch
    timeframe: str = "1"               # operating timeframe (Bybit interval: 1/5/15/60/240...) — the grid senses structure on these bars
    recenter_interval_s: float = 0.0   # live re-center cadence in seconds; 0 = derive from timeframe (bar/4, min 15s)
    max_inventory: str = "0"           # circuit-breaker net-position cap (0 = auto from grid size)
    max_drawdown: str = "0"            # circuit-breaker loss cap in quote units (0 = disabled)
    account_drawdown: str = "0"        # account-level kill-switch: wallet-equity drop cap in quote units (0 = disabled)

    # Mantle chain + deployed contracts
    mantle_rpc: str = ""
    mantle_private_key: str = ""  # signer for on-chain commit/attest/write (never commit)
    memory_detail_path: str = ""  # off-chain detail mirror for recall reconstruction
    iziswap_markets: str = ""      # JSON map for mantle_dex venue: {"WMNTUSDT": {base, quote, base_decimals, quote_decimals, fee}}
    strategy_ledger_addr: str = ""
    strategy_memory_addr: str = ""
    vault_addr: str = ""

    # Signals / inference (hackathon credits)
    elfa_api_key: str = ""
    nansen_api_key: str = ""
    surf_api_key: str = ""
    surf_base_url: str = "https://api.asksurf.ai/gateway/v1"

    # Scheduled macro events (CPI/FOMC/NFP), comma-separated ISO timestamps.
    # Launches inside [-30m, +90m] of an event are gated (agent/gates.py) —
    # the calendar is entered manually each week; the mechanism is automatic.
    news_events: str = ""

    store_db_path: str = "perpsagent.db"
    postgres_dsn: str = ""        # durable multi-tenant store; empty = use SQLite (store_db_path)
    cred_master_key: str = ""     # Fernet key for encrypting per-user venue keys (never commit/log)

    # Sharded engine plane (plan/SCALING.md #10). One worker process per node.
    shard_node: str = "0"         # this worker's node id (must be one of the shard set)
    shard_count: int = 1          # number of shards (worker builds ShardRouter(shard_count))
    worker_port: int = 9000       # this worker's HTTP port
    worker_host: str = "127.0.0.1"  # bind address; localhost-only by default — the worker API is
                                    # UNAUTHENTICATED, so never expose it publicly. Set 0.0.0.0 only
                                    # for a multi-host gateway deploy behind a firewall/private net.
    gateway_port: int = 8080      # gateway HTTP port
    shard_urls: str = ""          # gateway routing map, JSON {"0":"http://host:9000", ...}

    # Builder fee (monetization #1, docs/CONCEPT.md §7). A flat fee settled ON-CHAIN
    # from the operator's Vault bond to the treasury on each closed episode — "charge
    # for the system, not for PnL". Atomic units of fee_asset (USDC 6dp: 10000 = $0.01).
    # 0 = disabled. Requires the operator to hold FEE_MANAGER_ROLE (deployer does) and
    # to have deposited a bond of fee_asset (Vault.deposit); otherwise the settle is
    # logged-and-skipped, never fatal.
    builder_fee: int = 0
    fee_asset: str = ""        # ERC20 charged; falls back to x402_asset when empty
    fee_account: str = ""      # bond payer; falls back to the operator signer address

    # x402 alpha API (pay-per-call settlement)
    x402_pay_to: str = ""          # treasury wallet receiving USDC
    x402_asset: str = ""           # USDC token address on the target chain
    x402_network: str = "mantle-sepolia"
    x402_chain_id: int = 5003
    x402_price: str = "10000"      # atomic units (USDC 6dp -> $0.01/call)
    x402_facilitator_url: str = ""
    # Native-MNT settlement: pay MNT directly, verified on-chain (Mantle's gas token
    # isn't an EIP-3009 ERC-20, so the gasless "exact" scheme can't move it). Default
    # on — set false only to use an EIP-3009 ERC-20 via a facilitator instead.
    x402_native: bool = True
    alpha_port: int = 8402
    alpha_cache_ttl_s: float = 5.0  # TTL for regime/recall responses (0 disables)
    redis_url: str = ""             # shared alpha cache across replicas; empty = in-process

    def assert_consistent(self) -> None:
        """Refuse obvious env<->URL mismatches before any money path runs."""
        looks_testnet = any(t in self.mantle_rpc.lower() for t in ("sepolia", "testnet"))
        if self.env == "mainnet":
            if self.bybit_testnet:
                raise ValueError("env=mainnet but bybit_testnet=True")
            if self.proofs_on_testnet:
                if self.mantle_rpc and not looks_testnet:
                    raise ValueError("proofs_on_testnet=True but Mantle RPC is not a testnet")
            elif self.mantle_rpc and looks_testnet:
                raise ValueError(
                    "env=mainnet but Mantle RPC looks like testnet — set "
                    "PERPSAGENT_PROOFS_ON_TESTNET=true to keep proofs on a free testnet "
                    "chain while trading real money on Bybit"
                )
        else:
            if self.proofs_on_testnet:
                raise ValueError("proofs_on_testnet only applies when env=mainnet")
            if not self.bybit_testnet:
                raise ValueError(
                    "env=testnet but bybit_testnet=False — that is real Bybit money "
                    "under a testnet label; set PERPSAGENT_ENV=mainnet explicitly to trade live"
                )
            if self.mantle_rpc and not looks_testnet:
                raise ValueError("env=testnet but Mantle RPC does not look like testnet")
