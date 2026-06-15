"""Inline keyboards + callback-data factories. Pure (no I/O), so they're trivially
unit-testable. Style follows the reference inline UI (PR #48): InlineKeyboardBuilder
+ typed CallbackData, a shared 🏠 Menu · ❌ Close footer, a 🧹 Cancel on wizard steps.
Extended for this branch's logic: wallet, settings, and connect live on the menu too.
"""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class MenuCB(CallbackData, prefix="m"):
    # refresh | new_grid | grids | balance | price | wallet | topup | settings
    # | connect | disconnect | reset | help | close | back
    action: str


class WizCB(CallbackData, prefix="w"):
    field: str   # market | band | levels | size | confirm | cancel
    value: str


class PriceCB(CallbackData, prefix="pr"):
    market: str


class GridCB(CallbackData, prefix="g"):
    action: str  # pause | stop_ask | stop_do
    iid: str


class SetCB(CallbackData, prefix="s"):
    kind: str    # open (show value picker) | set (apply val) | custom (prompt typing)
    key: str
    val: str = ""


class PosCB(CallbackData, prefix="po"):
    action: str  # close_ask | close_do
    market: str


def _menu_row(kb: InlineKeyboardBuilder) -> None:
    """Standard footer: 🏠 Menu (back to main menu) · ❌ Close (delete this message)."""
    kb.row(
        InlineKeyboardButton(text="🏠 Menu", callback_data=MenuCB(action="back").pack()),
        InlineKeyboardButton(text="❌ Close", callback_data=MenuCB(action="close").pack()),
    )


def _cancel_row(kb: InlineKeyboardBuilder) -> None:
    kb.row(InlineKeyboardButton(
        text="🧹 Cancel", callback_data=WizCB(field="cancel", value="x").pack()))


def _b(text: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=MenuCB(action=action).pack())


def main_menu_kb(connected: bool) -> InlineKeyboardMarkup:
    """The one-screen home: a primary action up top, info shortcuts grouped below,
    account row flips on connection (Connect ↔ Re-connect/Disconnect)."""
    kb = InlineKeyboardBuilder()
    kb.row(_b("🚀 New Grid", "new_grid"))                       # primary, full width
    kb.row(_b("📊 My Grids", "grids"), _b("📋 Positions", "positions"))
    kb.row(_b("📑 Orders", "orders"), _b("📜 History", "history"))
    kb.row(_b("👛 Wallet", "wallet"), _b("💰 Balance", "balance"), _b("💱 Price", "price"))
    kb.row(_b("💧 Top up", "topup"))
    if connected:
        kb.row(_b("🔑 Re-connect", "connect"), _b("🗑 Disconnect", "disconnect"))
    else:
        kb.row(_b("🔑 Connect Bybit keys", "connect"))
    kb.row(_b("🔄 Refresh", "refresh"), _b("❓ Help", "help"))
    return kb.as_markup()


def back_kb(refresh: str | None = None) -> InlineKeyboardMarkup:
    """Section footer: an optional 🔄 Refresh (a MenuCB action to re-run) + Menu/Close."""
    kb = InlineKeyboardBuilder()
    if refresh:
        kb.button(text="🔄 Refresh", callback_data=MenuCB(action=refresh))
        kb.adjust(1)
    _menu_row(kb)
    return kb.as_markup()


# ── grid wizard pickers ──────────────────────────────────────────────────────

def _picker(field: str, options: list[tuple[str, str]], *, per_row: int,
            custom: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, value in options:
        kb.button(text=label, callback_data=WizCB(field=field, value=value))
    if custom:
        kb.button(text="✏️ Type custom", callback_data=WizCB(field=field, value="custom"))
    kb.adjust(per_row)
    _cancel_row(kb)
    return kb.as_markup()


def wiz_market_kb(coins: list[str]) -> InlineKeyboardMarkup:
    return _picker("market", [(c, c) for c in coins], per_row=2)


def wiz_strategy_kb() -> InlineKeyboardMarkup:
    """One-tap styles (no jargon) + a manual escape hatch."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🛡 Safe", callback_data=WizCB(field="preset", value="safe"))
    kb.button(text="⚖️ Balanced", callback_data=WizCB(field="preset", value="balanced"))
    kb.button(text="🔥 Aggressive", callback_data=WizCB(field="preset", value="aggressive"))
    kb.button(text="✏️ Set it myself", callback_data=WizCB(field="preset", value="custom"))
    kb.adjust(1)
    _cancel_row(kb)
    return kb.as_markup()


def wiz_margin_kb() -> InlineKeyboardMarkup:
    """How much balance to commit as margin — % of free balance, or a custom amount."""
    return _picker("margin", [("25%", "0.25"), ("50%", "0.5"), ("75%", "0.75"), ("100%", "1")],
                   per_row=4)


def wiz_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Launch", callback_data=WizCB(field="confirm", value="go"))
    kb.button(text="✖️ Cancel", callback_data=WizCB(field="cancel", value="x"))
    kb.adjust(2)
    return kb.as_markup()


def launched_kb(iid: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 My Grids", callback_data=MenuCB(action="grids"))
    kb.button(text="🛑 Stop", callback_data=GridCB(action="stop_ask", iid=iid))
    kb.adjust(2)
    _menu_row(kb)
    return kb.as_markup()


# ── grid list + per-grid actions ─────────────────────────────────────────────

def _short(iid: str) -> str:
    """Last hyphen segment of an instance id (BTCUSDT-0-ab12cd34 -> ab12cd34) for
    compact button labels; the full id still rides in the callback payload."""
    return iid.rsplit("-", 1)[-1]


_STATE_ICON = {"RUNNING": "🟢", "REBALANCING": "🔄", "HALTED": "🔴",
               "EXITING": "🟠", "INITIALIZING": "⏳"}


def grids_kb(rows: list[dict]) -> InlineKeyboardMarkup:
    """One tappable button per grid (→ detail/edit), then Refresh/Clear/History."""
    kb = InlineKeyboardBuilder()
    for r in rows:
        iid = r["instance_id"]
        icon = _STATE_ICON.get(r.get("state"), "•")
        label = r.get("name") or _short(iid)
        kb.row(InlineKeyboardButton(text=f"{icon} {label}",
                                    callback_data=GridCB(action="detail", iid=iid).pack()))
    kb.row(
        InlineKeyboardButton(text="🔄 Refresh", callback_data=MenuCB(action="grids").pack()),
        InlineKeyboardButton(text="🧹 Clear stopped", callback_data=MenuCB(action="clear").pack()),
    )
    kb.row(InlineKeyboardButton(text="📜 History", callback_data=MenuCB(action="history").pack()))
    _menu_row(kb)
    return kb.as_markup()


def detail_kb(iid: str, running: bool) -> InlineKeyboardMarkup:
    """Per-grid actions: rename, edit (stop+relaunch), pause (if running), stop."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Rename", callback_data=GridCB(action="rename", iid=iid))
    kb.button(text="🛠 Edit", callback_data=GridCB(action="edit", iid=iid))
    if running:
        kb.button(text="⏸ Pause", callback_data=GridCB(action="pause", iid=iid))
    kb.button(text="🛑 Stop", callback_data=GridCB(action="stop_ask", iid=iid))
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="⬅️ Grids", callback_data=MenuCB(action="grids").pack()))
    _menu_row(kb)
    return kb.as_markup()


def stop_confirm_kb(iid: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛑 Confirm Stop", callback_data=GridCB(action="stop_do", iid=iid))
    kb.button(text="✖️ Cancel", callback_data=MenuCB(action="grids"))
    kb.adjust(2)
    return kb.as_markup()


# ── price ────────────────────────────────────────────────────────────────────

def positions_kb(rows: list[dict]) -> InlineKeyboardMarkup:
    """Refresh + a Close button per open position, then the Menu/Close footer."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔄 Refresh", callback_data=MenuCB(action="positions").pack()))
    for r in rows:
        mkt = r["market"]
        kb.row(InlineKeyboardButton(text=f"❌ Close {mkt}",
                                    callback_data=PosCB(action="close_ask", market=mkt).pack()))
    kb.row(_b("❌ Close all", "closeall_ask"), _b("🧹 Cancel all orders", "cancelall_ask"))
    kb.row(_b("🛑 Close everything", "panic_ask"))
    _menu_row(kb)
    return kb.as_markup()


def orders_kb() -> InlineKeyboardMarkup:
    """Open-orders view: refresh + cancel-all (which also stops grids)."""
    kb = InlineKeyboardBuilder()
    kb.row(_b("🔄 Refresh", "orders"), _b("🧹 Cancel all", "cancelall_ask"))
    _menu_row(kb)
    return kb.as_markup()


def pos_confirm_kb(market: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Confirm Close", callback_data=PosCB(action="close_do", market=market))
    kb.button(text="✖️ Cancel", callback_data=MenuCB(action="positions"))
    kb.adjust(2)
    return kb.as_markup()


def bulk_confirm_kb(do_action: str) -> InlineKeyboardMarkup:
    """Yes/Cancel for a destructive bulk action (do_action is a MenuCB action)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Yes, do it", callback_data=MenuCB(action=do_action))
    kb.button(text="✖️ Cancel", callback_data=MenuCB(action="positions"))
    kb.adjust(2)
    return kb.as_markup()


def price_kb(coins: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in coins:
        kb.button(text=c, callback_data=PriceCB(market=c))
    kb.adjust(2)
    _menu_row(kb)
    return kb.as_markup()


def price_result_kb(market: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Refresh", callback_data=PriceCB(market=market))
    kb.button(text="💱 Other market", callback_data=MenuCB(action="price"))
    kb.adjust(2)
    _menu_row(kb)
    return kb.as_markup()


# ── grid knobs, set entirely by tapping in the builder — no typing required ──

# Friendly labels + tap-to-set presets per knob. Every picker also offers ✏️ Custom
# (reply with a value) for anything off-menu; the backend validates either way.
KNOB_LABEL = {
    "band": "Range %", "levels": "Steps", "size": "Size/step", "anchor": "Center",
    "leverage": "Leverage",
    "max_inventory": "Max inventory", "max_drawdown": "Max loss", "account_dd": "Account stop",
    "tp": "Take-profit", "trail": "Trailing", "trail_arm": "Trail arm", "bias": "Bias",
    "timeframe": "Timeframe", "recenter": "Re-center",
}

# Plain-language explanation shown above each value picker — what it means, the unit,
# and an example. Money knobs are in QUOTE units (USDT).
KNOB_HELP = {
    "band": "How far up & down from the current price the bot trades. <b>1 = ±1%</b>. "
            "Smaller = tighter range, trades more often.",
    "levels": "How many orders the bot spreads inside the range. More = finer grid, "
              "more frequent little trades.",
    "size": "How much of the coin each order uses (e.g. <code>0.001</code> BTC). "
            "Bigger = bigger position and bigger risk.",
    "anchor": "Where to put the grid's middle. <b>Smart</b> reads recent bars and leans "
              "toward the average — so it won't buy the top / sell the bottom. <b>Now</b> "
              "centers on the current price.",
    "leverage": "Multiplies your position size <b>and</b> risk. <b>x1</b> = no leverage "
                "(safest); <b>x10</b> = 10× exposure.",
    "max_inventory": "Safety cap on net position size. <b>0 = auto</b> (≈3× the grid). "
                     "When hit, the bot stops adding to the position.",
    "max_drawdown": "Auto-stop THIS grid if it loses this many <b>USDT</b>. <b>0 = off</b>. "
                    "e.g. <code>50</code> → cancel &amp; flatten at −50 USDT.",
    "account_dd": "Kill-switch for your WHOLE account: stop everything if wallet equity "
                  "drops this many <b>USDT</b>. <b>0 = off</b>.",
    "tp": "Bank the profit and close when this grid's total PnL reaches this many "
          "<b>USDT</b>. <b>0 = off</b>. e.g. <code>10</code> → take profit at +10 USDT.",
    "trail": "Lock gains: after a profit peak, close if PnL gives back this fraction. "
             "<b>0 = off</b>; <code>0.3</code> = give back 30% of the peak.",
    "trail_arm": "Profit (<b>USDT</b>) the grid must reach before trailing turns on. "
                 "<b>0</b> = arm immediately.",
    "bias": "Which way to lean. <b>Neutral</b> = both ways; <b>Long</b> = favor buys; "
            "<b>Short</b> = favor sells.",
    "timeframe": "The candle size the bot reads market structure on. <b>1m</b> = fast/scalpy, "
                 "<b>1h–4h</b> = calmer.",
    "recenter": "How often the bot re-centers the grid around the price. <b>Auto</b> = "
                "derived from the timeframe.",
}

KNOB_PRESETS: dict[str, list[tuple[str, str]]] = {
    "band": [("±0.5%", "0.5"), ("±1%", "1"), ("±2%", "2"), ("±5%", "5")],
    "levels": [("6", "6"), ("10", "10"), ("20", "20"), ("50", "50")],
    "size": [("0.001", "0.001"), ("0.005", "0.005"), ("0.01", "0.01"), ("0.05", "0.05")],
    "anchor": [("🧠 Smart (recent avg)", "mean"), ("Current price", "now")],
    "leverage": [("x1", "1"), ("x5", "5"), ("x10", "10"), ("x15", "15"), ("x20", "20"), ("x25", "25")],
    "bias": [("Neutral", "neutral"), ("Long", "long"), ("Short", "short")],
    "timeframe": [("1m", "1m"), ("5m", "5m"), ("15m", "15m"), ("1h", "1h"), ("4h", "4h"), ("1d", "1d")],
    "recenter": [("Auto", "auto"), ("15s", "15"), ("30s", "30"), ("60s", "60")],
    "max_inventory": [("Auto", "0")],
    "max_drawdown": [("Off", "0"), ("10", "10"), ("50", "50"), ("100", "100")],
    "account_dd": [("Off", "0"), ("50", "50"), ("100", "100"), ("500", "500")],
    "tp": [("Off", "0"), ("5", "5"), ("10", "10"), ("50", "50")],
    "trail": [("Off", "0"), ("0.2", "0.2"), ("0.3", "0.3"), ("0.5", "0.5")],
    "trail_arm": [("Off", "0"), ("5", "5"), ("10", "10")],
}


def grid_build_kb(settings: dict) -> InlineKeyboardMarkup:
    """The 'Set it myself' builder: one tap-to-edit button per knob (shows its value),
    then 🚀 Launch. Same picker UX as before, but it builds THIS grid."""
    kb = InlineKeyboardBuilder()
    for key in settings:
        label = KNOB_LABEL.get(key, key)
        kb.button(text=f"{label}: {settings[key]}", callback_data=SetCB(kind="open", key=key))
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="🚀 Launch grid", callback_data=WizCB(field="launch", value="x").pack()))
    kb.row(
        InlineKeyboardButton(text="↩️ Defaults", callback_data=WizCB(field="defaults", value="x").pack()),
        InlineKeyboardButton(text="⬅️ Styles", callback_data=WizCB(field="back", value="strategy").pack()),
        InlineKeyboardButton(text="🧹 Cancel", callback_data=WizCB(field="cancel", value="x").pack()),
    )
    return kb.as_markup()


def setting_picker_kb(key: str, current: str) -> InlineKeyboardMarkup:
    """Tap a preset value (current marked ✅), ✏️ Custom to type, or ⬅️ Back to the builder."""
    kb = InlineKeyboardBuilder()
    for label, val in KNOB_PRESETS.get(key, []):
        mark = "✅ " if str(val) == str(current) else ""
        kb.button(text=f"{mark}{label}", callback_data=SetCB(kind="set", key=key, val=val))
    kb.adjust(3)
    kb.row(
        InlineKeyboardButton(text="✏️ Custom", callback_data=SetCB(kind="custom", key=key).pack()),
        InlineKeyboardButton(text="⬅️ Back", callback_data=MenuCB(action="build").pack()),
    )
    return kb.as_markup()
