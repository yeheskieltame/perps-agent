"""Inline keyboards + callback-data factories. Pure (no I/O), so they're trivially
unit-testable. Style follows the reference UI: InlineKeyboardBuilder + typed
CallbackData, a shared 🏠 Menu · ❌ Close footer, and a 🧹 Cancel on wizard steps.
"""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class MenuCB(CallbackData, prefix="m"):
    # refresh | new_grid | grids | balance | price | help | close | back
    action: str


class WizCB(CallbackData, prefix="w"):
    field: str   # market | band | levels | size | confirm | cancel
    value: str


class PriceCB(CallbackData, prefix="pr"):
    market: str


class GridCB(CallbackData, prefix="g"):
    action: str  # pause | stop_ask | stop_do
    iid: str


# Common Bybit linear symbols offered as one-tap presets in the wizard + price view.
COINS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "SUIUSDT")


def _menu_row(kb: InlineKeyboardBuilder) -> None:
    """Standard footer: 🏠 Menu (back to main menu) · ❌ Close (delete this message)."""
    kb.row(
        InlineKeyboardButton(text="🏠 Menu", callback_data=MenuCB(action="back").pack()),
        InlineKeyboardButton(text="❌ Close", callback_data=MenuCB(action="close").pack()),
    )


def _cancel_row(kb: InlineKeyboardBuilder) -> None:
    kb.row(InlineKeyboardButton(
        text="🧹 Cancel", callback_data=WizCB(field="cancel", value="x").pack()))


def main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Refresh", callback_data=MenuCB(action="refresh"))
    kb.button(text="➕ New Grid", callback_data=MenuCB(action="new_grid"))
    kb.button(text="📊 My Grids", callback_data=MenuCB(action="grids"))
    kb.button(text="💰 Balance", callback_data=MenuCB(action="balance"))
    kb.button(text="💱 Price", callback_data=MenuCB(action="price"))
    kb.button(text="ℹ️ Help", callback_data=MenuCB(action="help"))
    kb.button(text="❌ Close", callback_data=MenuCB(action="close"))
    kb.adjust(2, 2, 2, 1)
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


def wiz_market_kb() -> InlineKeyboardMarkup:
    return _picker("market", [(c, c) for c in COINS], per_row=2)


def wiz_band_kb() -> InlineKeyboardMarkup:
    return _picker("band", [("±0.5%", "0.5"), ("±1%", "1"), ("±2%", "2"), ("±5%", "5")],
                   per_row=4)


def wiz_levels_kb() -> InlineKeyboardMarkup:
    return _picker("levels", [(v, v) for v in ("6", "10", "20", "50")], per_row=4)


def wiz_size_kb() -> InlineKeyboardMarkup:
    return _picker("size", [(v, v) for v in ("0.001", "0.005", "0.01", "0.05")], per_row=4)


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


def grids_kb(rows: list[dict]) -> InlineKeyboardMarkup:
    """One row per grid: ⏸ Pause (hidden once winding down) + 🛑 Stop, then Refresh."""
    kb = InlineKeyboardBuilder()
    for r in rows:
        iid = r["instance_id"]
        short = _short(iid)
        buttons = []
        if r.get("state") not in ("HALTED", "EXITING"):
            buttons.append(InlineKeyboardButton(
                text=f"⏸ Pause {short}", callback_data=GridCB(action="pause", iid=iid).pack()))
        buttons.append(InlineKeyboardButton(
            text=f"🛑 Stop {short}", callback_data=GridCB(action="stop_ask", iid=iid).pack()))
        kb.row(*buttons)
    kb.row(InlineKeyboardButton(text="🔄 Refresh", callback_data=MenuCB(action="grids").pack()))
    _menu_row(kb)
    return kb.as_markup()


def stop_confirm_kb(iid: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛑 Confirm Stop", callback_data=GridCB(action="stop_do", iid=iid))
    kb.button(text="✖️ Cancel", callback_data=MenuCB(action="grids"))
    kb.adjust(2)
    return kb.as_markup()


# ── price ────────────────────────────────────────────────────────────────────

def price_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in COINS:
        kb.button(text=c, callback_data=PriceCB(market=c))
    kb.adjust(2, 2)
    _menu_row(kb)
    return kb.as_markup()


def price_result_kb(market: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Refresh", callback_data=PriceCB(market=market))
    kb.button(text="💱 Other market", callback_data=MenuCB(action="price"))
    kb.adjust(2)
    _menu_row(kb)
    return kb.as_markup()
