"""Settings. Testnet by default; refuse to start on env<->URL mismatch
(mirrors deltaperps). Never hardcode keys — load from .env."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PERPSAGENT_", env_file=".env", extra="ignore")

    env: str = "testnet"  # testnet | mainnet

    # Bybit (user-supplied keys; never commit)
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = True

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

    store_db_path: str = "perpsagent.db"

    # x402 alpha API (pay-per-call settlement)
    x402_pay_to: str = ""          # treasury wallet receiving USDC
    x402_asset: str = ""           # USDC token address on the target chain
    x402_network: str = "mantle-sepolia"
    x402_chain_id: int = 5003
    x402_price: str = "10000"      # atomic units (USDC 6dp -> $0.01/call)
    x402_facilitator_url: str = ""
    alpha_port: int = 8402

    def assert_consistent(self) -> None:
        """Refuse obvious env<->URL mismatches before any money path runs."""
        looks_testnet = any(t in self.mantle_rpc.lower() for t in ("sepolia", "testnet"))
        if self.env == "mainnet":
            if self.bybit_testnet:
                raise ValueError("env=mainnet but bybit_testnet=True")
            if self.mantle_rpc and looks_testnet:
                raise ValueError("env=mainnet but Mantle RPC looks like testnet")
        elif self.mantle_rpc and not looks_testnet:
            raise ValueError("env=testnet but Mantle RPC does not look like testnet")
