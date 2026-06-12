"""One-screen dashboard UI — pure keyboard/text builders, NO aiogram imports.

Everything the bot can do is visible on a single message: the text is a live
summary (connection, equity, grids) and the inline keyboard always carries the
full menu. Actions that need a choice (market, instance, knob value) inject
context rows ABOVE the main menu — the user never walks a multi-step wizard.

A "row" is list[tuple[label, callback_data]]; main.py turns rows into
InlineKeyboardMarkup. Callback data is namespaced and stays well under
Telegram's 64-byte limit:

  d:<action>   dashboard navigation (home/launch/stopmenu/settings/price/help/...)
  g:<market>   launch a grid on market
  p:<market>   show top-of-book for market
  x:<iid>      stop instance
  s:<key>      cycle an enumerable setting to its next preset
  t:<key>      numeric setting — bot answers a toast with the exact /set command
"""
from __future__ import annotations

Rows = list[list[tuple[str, str]]]

DEFAULT_MARKETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "MNTUSDT"]

# Enumerable knobs cycle through sensible presets on tap (saved instantly).
# Free-numeric knobs (size, tp, ...) get a toast with the exact /set command —
# a button per knob keeps them on-screen without a wizard.
CYCLE: dict[str, list[str]] = {
    "band": ["0.5", "1", "1.5", "2", "3", "5"],
    "levels": ["4", "6", "8", "10", "12", "20"],
    "leverage": ["1", "3", "5", "10", "25", "50"],
    "bias": ["neutral", "long", "short"],
    "timeframe": ["1m", "5m", "15m", "1h", "4h", "1d"],
    "recenter": ["auto", "0", "30", "60", "300"],
}
TYPED = ["size", "tp", "trail", "trail_arm", "max_inventory", "max_drawdown", "account_dd"]


def next_value(key: str, current: str) -> str:
    """Next preset in the cycle; unknown current values restart the cycle."""
    presets = CYCLE[key]
    try:
        return presets[(presets.index(current) + 1) % len(presets)]
    except ValueError:
        return presets[0]


def menu_rows(connected: bool) -> Rows:
    """The always-visible main menu."""
    account = ([("🔑 Connect keys", "d:connect")] if not connected
               else [("🔑 Re-connect", "d:connect"), ("🗑 Disconnect", "d:disconnect")])
    return [
        [("🚀 Launch grid", "d:launch"), ("⏹ Stop…", "d:stopmenu")],
        [("⚙️ Settings", "d:settings"), ("💱 Price", "d:price")],
        [("🔄 Refresh", "d:home"), ("❓ Help", "d:help")],
        account,
    ]


def _grid2(buttons: list[tuple[str, str]]) -> Rows:
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def market_rows(markets: list[str], prefix: str) -> Rows:
    """Market pick injected above the menu. prefix: 'g' launch, 'p' price."""
    return _grid2([(m, f"{prefix}:{m}") for m in markets]) + [[("✖ Cancel", "d:home")]]


def stop_rows(instances: list[str]) -> Rows:
    return [[(f"⏹ {iid}", f"x:{iid}")] for iid in instances] + [[("✖ Cancel", "d:home")]]


def settings_rows(settings: dict) -> Rows:
    """Every knob on screen with its current value. Tap = cycle (enumerables)
    or toast the exact /set command (numerics)."""
    cycle = [(f"{k} {settings.get(k, '?')}", f"s:{k}") for k in CYCLE]
    typed = [(f"{k} {settings.get(k, '?')} ✏️", f"t:{k}") for k in TYPED]
    return (_grid2(cycle) + _grid2(typed)
            + [[("↩️ Reset all", "d:reset"), ("🏠 Back", "d:home")]])


def back_rows() -> Rows:
    return [[("🏠 Back", "d:home")]]


# ---- dashboard text ----

_STATE_ICON = {"RUNNING": "🟢", "REBALANCING": "🔄", "HALTED": "🔴",
               "EXITING": "🟠", "INITIALIZING": "⏳"}


def dashboard_text(creds: dict | None, balance: dict | None, grids: list[dict],
                   note: str = "") -> str:
    """The home screen: live summary, one glance. `note` is a one-line result of
    the last action (launched/stopped/error), shown on top."""
    lines = []
    if note:
        lines += [note, ""]
    lines.append("🤖 <b>Perps Agent</b> — verifiable grid trading")
    if creds and creds.get("connected"):
        env = "testnet" if creds.get("testnet", True) else "⚠️ MAINNET"
        key = creds.get("key_preview", "?")
        bal = (f" · 💰 {balance['equity']} {balance.get('currency', 'USDT')}"
               if balance else "")
        lines.append(f"🔑 Bybit {env} (<code>{key}</code>){bal}")
    else:
        lines.append("🔌 Not connected — tap <b>Connect keys</b> to link Bybit")
    if grids:
        lines.append("")
        for g in grids:
            icon = _STATE_ICON.get(g["state"], "•")
            lines.append(f"{icon} <code>{g['instance_id']}</code> — {g['state']}"
                         f" · pnl {g['realized_pnl']} · fills {g['fill_count']}")
    else:
        lines.append("\n📭 No grids running — tap <b>Launch grid</b>")
    return "\n".join(lines)
