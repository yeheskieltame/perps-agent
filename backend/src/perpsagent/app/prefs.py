"""User-tunable strategy knobs — ONE registry for validation, defaults, and the
guard objects they build.

Every knob the CLI runner exposes is adjustable per user through the GridService
seam (worker `/v1/settings` + `POST /v1/grids` overrides), so the Telegram bot can
tune the engine without importing it. Values are stored as user-facing strings
(band in PERCENT, timeframe as '1m'/'1h', ...) and converted to engine units here
— the UI never does unit math.

Precedence at launch: built-in defaults < user's saved settings < per-launch
overrides. Unknown keys and out-of-range values are rejected with a message the
bot can show verbatim.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, InvalidOperation

from ..agent.sense import tf_minutes
from .safety import AccountGuard, CircuitBreaker, ProfitGuard

# Human timeframes -> Bybit interval codes (mirrors the runner's alias map).
TF_ALIAS = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
            "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720", "1d": "D"}
_TF_CODES = set(TF_ALIAS.values())

BIAS_VALUES = {"neutral": 0, "long": 1, "short": -1}

# key -> (default, validator). Validators normalize to a canonical string or
# raise ValueError with a user-facing reason.
ALIASES = {"lev": "leverage", "tf": "timeframe", "inv": "max_inventory",
           "dd": "max_drawdown", "arm": "trail_arm", "acct_dd": "account_dd"}


def _dec(raw: str, lo: Decimal, hi: Decimal | None, what: str) -> str:
    try:
        v = Decimal(str(raw).strip())
    except InvalidOperation:
        raise ValueError(f"{what}: not a number: {raw!r}") from None
    if v < lo or (hi is not None and v > hi):
        rng = f">= {lo}" if hi is None else f"in [{lo}, {hi}]"
        raise ValueError(f"{what}: must be {rng}, got {v}")
    return str(v.normalize())


def _v_band(raw: str) -> str:
    return _dec(raw, Decimal("0.05"), Decimal("10"), "band (percent, e.g. 1 = ±1%)")


def _v_levels(raw: str) -> str:
    try:
        n = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"levels: not an integer: {raw!r}") from None
    if not 2 <= n <= 200:
        raise ValueError(f"levels: must be 2..200, got {n}")
    return str(n)


def _v_size(raw: str) -> str:
    return _dec(raw, Decimal("0.000001"), None, "size (base qty per level)")


def _v_leverage(raw: str) -> str:
    return _dec(raw, Decimal(1), Decimal(100), "leverage")


def _v_bias(raw: str) -> str:
    v = str(raw).strip().lower()
    if v not in BIAS_VALUES:
        raise ValueError(f"bias: choose long, short or neutral (got {raw!r})")
    return v


def _v_timeframe(raw: str) -> str:
    v = str(raw).strip().lower()
    if v in TF_ALIAS:
        return v
    if v.upper() in _TF_CODES or v.upper() == "D":
        return v.upper()
    raise ValueError(f"timeframe: use one of {', '.join(TF_ALIAS)} (got {raw!r})")


def _v_quote(what: str):
    return lambda raw: _dec(raw, Decimal(0), None, f"{what} (quote units; 0 = off)")


def _v_trail(raw: str) -> str:
    return _dec(raw, Decimal(0), Decimal("0.9"), "trail (fraction of peak given back; 0 = off)")


def _v_recenter(raw: str) -> str:
    v = str(raw).strip().lower()
    if v == "auto":
        return "auto"
    return _dec(v, Decimal(0), Decimal(86400), "recenter (seconds; 0 = off, or 'auto')")


KNOBS: dict[str, tuple[str, object]] = {
    # grid shape
    "band": ("1", _v_band),                  # ± percent around mid
    "levels": ("10", _v_levels),
    "size": ("0.001", _v_size),              # base qty per level
    # risk
    "leverage": ("1", _v_leverage),
    "max_inventory": ("0", _v_quote("max_inventory")),   # 0 = auto from grid size
    "max_drawdown": ("0", _v_quote("max_drawdown")),     # 0 = off
    "account_dd": ("0", _v_quote("account_dd")),         # wallet-equity kill-switch
    # exit
    "tp": ("0", _v_quote("tp")),             # bank when total PnL >= tp
    "trail": ("0", _v_trail),
    "trail_arm": ("0", _v_quote("trail_arm")),
    # behavior
    "bias": ("neutral", _v_bias),
    "timeframe": ("1m", _v_timeframe),
    "recenter": ("auto", _v_recenter),       # supervisor cadence; auto = bar/4
}


# ---- one-tap strategy templates (auto-sized to the user's balance) ----
# margin = fraction of FREE balance to commit as margin; notional = margin × leverage;
# size per level = notional / (levels × price). band is the knob percent (1 = ±1%).
# Starting defaults — meant to be tuned by a testnet soak (see deploy/README.md).
STRATEGY_TEMPLATES: dict[str, dict] = {
    "safe":       {"band": "0.5", "levels": 10, "leverage": "1",  "margin": "0.15"},
    "balanced":   {"band": "1",   "levels": 10, "leverage": "5",  "margin": "0.35"},
    "aggressive": {"band": "2",   "levels": 8,  "leverage": "25", "margin": "0.80"},
}


def grid_size(template: str, margin_quote, mid) -> Decimal:
    """Base qty per level for an explicit `margin` (quote/USDT the user commits):
    notional = margin × leverage; size = notional / (levels × price). Floors to 6dp
    (never over-sizes); 0 when margin/price are non-positive."""
    tpl = STRATEGY_TEMPLATES[template]
    margin, price = Decimal(str(margin_quote)), Decimal(str(mid))
    if margin <= 0 or price <= 0:
        return Decimal(0)
    notional = margin * Decimal(tpl["leverage"])
    return (notional / (Decimal(tpl["levels"]) * price)).quantize(
        Decimal("0.000001"), rounding=ROUND_DOWN)


def default_margin(template: str, free_balance) -> Decimal:
    """The template's default margin = its margin fraction of free balance."""
    return Decimal(str(free_balance)) * Decimal(STRATEGY_TEMPLATES[template]["margin"])


def autosize(template: str, free_balance, mid) -> Decimal:
    """Size for the template's DEFAULT margin (margin% of free balance)."""
    return grid_size(template, default_margin(template, free_balance), mid)


def template_settings(template: str, size: Decimal) -> dict[str, str]:
    """The validated knob overrides a template produces for create_grid."""
    tpl = STRATEGY_TEMPLATES[template]
    return {"band": tpl["band"], "levels": str(tpl["levels"]),
            "leverage": tpl["leverage"], "size": str(size)}


def normalize_key(key: str) -> str:
    k = str(key).strip().lower().replace("-", "_")
    return ALIASES.get(k, k)


def validate_updates(updates: dict) -> dict[str, str]:
    """Normalize keys and validate values. Raises ValueError naming the bad key."""
    out: dict[str, str] = {}
    for key, value in updates.items():
        k = normalize_key(key)
        if k not in KNOBS:
            raise ValueError(f"unknown setting {key!r} — valid: {', '.join(KNOBS)}")
        out[k] = KNOBS[k][1](value)
    return out


def merged(saved: dict | None, overrides: dict | None = None) -> dict[str, str]:
    """defaults < saved < overrides. `saved` is trusted (validated at PUT time);
    `overrides` must already be validated by the caller."""
    out = {k: d for k, (d, _) in KNOBS.items()}
    out.update({k: str(v) for k, v in (saved or {}).items() if k in KNOBS})
    out.update(overrides or {})
    return out


# ---- engine-unit conversions (the only place UI strings meet engine types) ----

def band_fraction(s: dict) -> Decimal:
    return Decimal(s["band"]) / 100


def grid_fields(s: dict) -> dict:
    """GridConfig kwargs derived from settings."""
    return {"levels": int(s["levels"]), "order_size": Decimal(s["size"]),
            "leverage": Decimal(s["leverage"]), "bias": BIAS_VALUES[s["bias"]]}


def timeframe_code(s: dict) -> str:
    return TF_ALIAS.get(s["timeframe"], s["timeframe"])


def monitor_interval(s: dict) -> float:
    """Supervisor cadence: explicit seconds, or bar/4 floored at 15s (runner rule)."""
    if s["recenter"] != "auto":
        return float(s["recenter"])
    return max(15.0, tf_minutes(timeframe_code(s)) * 60 / 4)


def build_guards(s: dict) -> tuple[CircuitBreaker, ProfitGuard, AccountGuard | None]:
    # 0 = auto inventory cap: 3x nominal one-sided inventory (same rule as the
    # agent loop, which the worker path bypasses).
    inv = Decimal(s["max_inventory"])
    if inv <= 0:
        inv = Decimal(s["size"]) * int(s["levels"]) * 3
    breaker = CircuitBreaker(max_inventory=inv, max_drawdown=Decimal(s["max_drawdown"]))
    profit = ProfitGuard(take_profit=Decimal(s["tp"]), trail_frac=Decimal(s["trail"]),
                         trail_arm=Decimal(s["trail_arm"]))
    account = AccountGuard(max_drop=Decimal(s["account_dd"])) if Decimal(s["account_dd"]) > 0 else None
    return breaker, profit, account
