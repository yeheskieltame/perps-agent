"""Bot settings. The bot is a thin client: a token, the backend base URL, and an
allowlist. NEVER hardcode the token — load from .env (gitignored)."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PERPSBOT_", env_file=".env", extra="ignore")

    token: str = ""                          # @BotFather token (required to run)
    api_url: str = "http://127.0.0.1:9000"   # worker (dev) or gateway (prod)
    allowlist: str = ""                      # comma-separated user ids; empty = allow all (dev)
    markets: str = "BTCUSDT,ETHUSDT,SOLUSDT,MNTUSDT"  # dashboard quick-pick buttons
    # grid defaults live BACKEND-side now (per-user /settings — backend app/prefs.py)

    def allowed_ids(self) -> set[int]:
        return {int(x) for x in self.allowlist.split(",") if x.strip()}

    def market_list(self) -> list[str]:
        return [m.strip().upper() for m in self.markets.split(",") if m.strip()]


def is_allowed(user_id: int | None, allow: set[int]) -> bool:
    """Empty allowlist = open (dev). Otherwise the sender id must be listed."""
    if not allow:
        return True
    return user_id is not None and user_id in allow
