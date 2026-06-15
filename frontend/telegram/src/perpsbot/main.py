"""Entrypoint — aiogram v3 wiring. Inline-keyboard UI (PR #48 style) over this
branch's logic: the one-screen home (snapshot + buttons), the /grid wizard, per-grid
actions, plus /connect (FSM), /settings, and the managed MNT wallet. Handlers stay
thin over the pure renderers in `commands`; this module owns only Telegram plumbing.

    cd frontend/telegram && cp .env.example .env   # fill PERPSBOT_TOKEN
    python -m perpsbot.main
"""
from __future__ import annotations

import asyncio
import contextlib

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BufferedInputFile, CallbackQuery, ForceReply, Message

from . import commands, pnlcard
from .api import WorkerAPI
from .config import BotSettings, is_allowed
from .keyboards import (
    KNOB_HELP, KNOB_LABEL, GridCB, MenuCB, PosCB, PriceCB, SetCB, WizCB,
    back_kb, bulk_confirm_kb, detail_kb, grid_build_kb, grids_kb, launched_kb, main_menu_kb,
    orders_kb, pos_confirm_kb, positions_kb, price_kb, price_result_kb, setting_picker_kb,
    stop_confirm_kb, wiz_confirm_kb, wiz_margin_kb, wiz_market_kb, wiz_strategy_kb,
)
from .wizard import GridWizard, parse_market

router = Router()

_COMMANDS = [
    BotCommand(command="start", description="🏠 Home — balance, positions, all buttons"),
    BotCommand(command="grid", description="➕ Launch a grid (wizard or args)"),
    BotCommand(command="status", description="📊 Your grids"),
    BotCommand(command="positions", description="📋 Live Bybit positions"),
    BotCommand(command="orders", description="📑 Open Bybit orders"),
    BotCommand(command="history", description="📜 Closed-grid history"),
    BotCommand(command="wallet", description="👛 Your MNT wallet"),
    BotCommand(command="topup", description="💧 Fund your MNT wallet"),
    BotCommand(command="balance", description="💰 Venue equity"),
    BotCommand(command="price", description="💱 Live top-of-book"),
    BotCommand(command="connect", description="🔑 Link Bybit API keys (DM)"),
    BotCommand(command="help", description="ℹ️ Help"),
]


def _args(m: Message) -> str:
    return (m.text or "").partition(" ")[2].strip()


def _uid(event: Message | CallbackQuery) -> int:
    assert event.from_user is not None
    return event.from_user.id


class Connect(StatesGroup):
    """The /connect dialog: collect key → secret → environment, then store."""

    key = State()
    secret = State()
    env = State()


class Configure(StatesGroup):
    """A single custom config value: tap ✏️ Custom → reply with the value."""

    value = State()


class Rename(StatesGroup):
    """Naming a grid: tap ✏️ Rename → reply with the name."""

    name = State()


class Allowlist(BaseMiddleware):
    """Drop messages/callbacks from senders outside the allowlist (empty = open/dev)."""

    def __init__(self, allow: set[int]) -> None:
        self.allow = allow

    async def __call__(self, handler, event, data):
        uid = event.from_user.id if event.from_user else None
        if not is_allowed(uid, self.allow):
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ Access denied.", show_alert=True)
            else:
                await event.answer("⛔ Access denied.")
            return None
        return await handler(event, data)


# ── home + small helpers ─────────────────────────────────────────────────────

async def _connected(api: WorkerAPI, user_id: int) -> bool:
    try:
        c = await api.get_credentials(user_id)
        return bool(c and c.get("connected"))
    except Exception:  # noqa: BLE001 — a transport blip just shows the not-connected menu
        return False


async def _home_text_kb(api: WorkerAPI, user_id: int):
    text = await commands.menu_snapshot(api, user_id)
    return text, main_menu_kb(await _connected(api, user_id))


async def _send_home(message: Message, api: WorkerAPI, user_id: int) -> None:
    text, kb = await _home_text_kb(api, user_id)
    await message.answer(text, reply_markup=kb)


async def _edit(cb: CallbackQuery, text: str, kb) -> None:
    if isinstance(cb.message, Message):
        with contextlib.suppress(TelegramBadRequest):  # "message is not modified" is fine
            await cb.message.edit_text(text, reply_markup=kb)


async def _edit_home(cb: CallbackQuery, api: WorkerAPI) -> None:
    text, kb = await _home_text_kb(api, _uid(cb))
    await _edit(cb, text, kb)


async def _delete(cb: CallbackQuery) -> None:
    if isinstance(cb.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.delete()


async def _scrub(m: Message) -> str:
    """Delete a message carrying a secret; if Telegram refuses, ask the user to."""
    try:
        await m.delete()
        return ""
    except Exception:  # noqa: BLE001
        return "\n⚠️ I could not delete your last message — please delete it manually."


# ── typed commands (all keep working alongside the buttons) ──────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, api: WorkerAPI, state: FSMContext) -> None:
    await state.clear()
    await _send_home(message, api, _uid(message))


@router.message(Command("menu"))
async def cmd_menu(message: Message, api: WorkerAPI, state: FSMContext) -> None:
    await state.clear()
    await _send_home(message, api, _uid(message))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(commands.HELP, reply_markup=back_kb())


@router.message(Command("grid"))
async def cmd_grid(message: Message, api: WorkerAPI, settings: BotSettings,
                   state: FSMContext) -> None:
    args = _args(message)
    if args:  # power path: /grid BTCUSDT band=1 levels=12 still works
        await message.answer(await commands.grid(api, _uid(message), args))
    else:
        await _wizard_start(message, settings, state)


@router.message(Command("status"))
async def cmd_status(message: Message, api: WorkerAPI) -> None:
    rows = await _safe_status(api, _uid(message))
    await message.answer(commands.render_status(rows), reply_markup=grids_kb(rows))


@router.message(Command("history"))
async def cmd_history(message: Message, api: WorkerAPI) -> None:
    await message.answer(await commands.history(api, _uid(message)), reply_markup=back_kb("history"))


@router.message(Command("stop"))
async def cmd_stop(message: Message, api: WorkerAPI) -> None:
    args = _args(message)
    if args:
        await message.answer(await commands.stop(api, _uid(message), args))
    else:
        rows = await _safe_status(api, _uid(message))
        await message.answer("Tap a grid's 🛑 to stop it.", reply_markup=grids_kb(rows))


@router.message(Command("pause"))
async def cmd_pause(message: Message, api: WorkerAPI) -> None:
    args = _args(message)
    if args:
        await message.answer(await commands.pause(api, _uid(message), args))
    else:
        rows = await _safe_status(api, _uid(message))
        await message.answer("Tap a grid's ⏸ to pause it.", reply_markup=grids_kb(rows))


@router.message(Command("price"))
async def cmd_price(message: Message, api: WorkerAPI, settings: BotSettings) -> None:
    args = _args(message)
    if args:
        market = args.split()[0].upper()
        await message.answer(await commands.price(api, _uid(message), market),
                             reply_markup=price_result_kb(market))
    else:
        await message.answer("💱 <b>Price</b> — pick a market:",
                             reply_markup=price_kb(settings.market_list()))


@router.message(Command("balance"))
async def cmd_balance(message: Message, api: WorkerAPI) -> None:
    await message.answer(await commands.balance(api, _uid(message)), reply_markup=back_kb("balance"))


@router.message(Command("positions"))
async def cmd_positions(message: Message, api: WorkerAPI) -> None:
    text, rows = await commands.positions(api, _uid(message))
    await message.answer(text, reply_markup=positions_kb(rows) if rows is not None else back_kb("positions"))


@router.message(Command("orders"))
async def cmd_orders(message: Message, api: WorkerAPI) -> None:
    await message.answer(await commands.orders(api, _uid(message)), reply_markup=orders_kb())


@router.message(Command("wallet"))
async def cmd_wallet(message: Message, api: WorkerAPI) -> None:
    await message.answer(await commands.wallet(api, _uid(message)), reply_markup=back_kb("wallet"))


@router.message(Command("topup"))
async def cmd_topup(message: Message, api: WorkerAPI) -> None:
    await message.answer(await commands.topup(api, _uid(message)), reply_markup=back_kb("topup"))


# ── /connect dialog (DM-only FSM) ────────────────────────────────────────────

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(commands.CONNECT_CANCELLED)


@router.message(Command("connect"))
async def cmd_connect(message: Message, state: FSMContext, api: WorkerAPI) -> None:
    if message.chat.type != "private":
        await message.answer(commands.CONNECT_DM_ONLY)
        return
    await state.set_state(Connect.key)
    await message.answer(await commands.connect_start(api, _uid(message)))


@router.message(Command("disconnect"))
async def cmd_disconnect(message: Message, api: WorkerAPI) -> None:
    await message.answer(await commands.disconnect(api, _uid(message)))


@router.message(Connect.key)
async def on_connect_key(m: Message, state: FSMContext) -> None:
    key = (m.text or "").strip()
    warn = await _scrub(m)
    if not key:
        await m.answer("Send the API key as plain text, or /cancel." + warn)
        return
    await state.update_data(key=key)
    await state.set_state(Connect.secret)
    await m.answer(commands.CONNECT_ASK_SECRET + warn)


@router.message(Connect.secret)
async def on_connect_secret(m: Message, state: FSMContext) -> None:
    secret = (m.text or "").strip()
    warn = await _scrub(m)
    if not secret:
        await m.answer("Send the API secret as plain text, or /cancel." + warn)
        return
    await state.update_data(secret=secret)
    await state.set_state(Connect.env)
    await m.answer(commands.CONNECT_ASK_ENV + warn)


@router.message(Connect.env)
async def on_connect_env(m: Message, state: FSMContext, api: WorkerAPI) -> None:
    choice = commands.parse_env_choice(m.text or "")
    if choice is None:
        await m.answer(commands.CONNECT_BAD_ENV)
        return
    data = await state.get_data()
    await state.clear()
    await m.answer(await commands.connect_finish(api, _uid(m), data["key"], data["secret"], testnet=choice))
    await _send_home(m, api, _uid(m))  # land back on the home screen


# ── main-menu callbacks (one handler, dispatched by action) ──────────────────

async def _safe_status(api: WorkerAPI, user_id: int) -> list[dict]:
    with contextlib.suppress(Exception):
        return list(await api.status(user_id))
    return []


@router.callback_query(MenuCB.filter())
async def cb_menu(cb: CallbackQuery, api: WorkerAPI, state: FSMContext,
                  settings: BotSettings, callback_data: MenuCB) -> None:
    action = callback_data.action
    uid = _uid(cb)
    if action == "close":
        await cb.answer()
        await _delete(cb)
        return
    if action == "back":
        await state.clear()
        await cb.answer()
        await _delete(cb)
        await _send_home(cb.message, api, uid)
        return
    if action == "connect":
        await cb.answer()
        if isinstance(cb.message, Message) and cb.message.chat.type != "private":
            await cb.answer("🔒 DM only — open a private chat with me.", show_alert=True)
            return
        await state.set_state(Connect.key)
        if isinstance(cb.message, Message):
            await cb.message.answer(await commands.connect_start(api, uid))
        return

    await cb.answer()
    if action in ("refresh", "disconnect"):
        if action == "disconnect":
            await commands.disconnect(api, uid)
        await _edit_home(cb, api)
    elif action == "new_grid":
        if isinstance(cb.message, Message):
            await _wizard_start(cb.message, settings, state)
    elif action == "grids":
        rows = await _safe_status(api, uid)
        await _edit(cb, commands.render_status(rows), grids_kb(rows))
    elif action == "history":
        await _edit(cb, await commands.history(api, uid), back_kb("history"))
    elif action == "clear":
        note = await commands.clear_stopped(api, uid)
        rows = await _safe_status(api, uid)
        await _edit(cb, note + "\n\n" + commands.render_status(rows), grids_kb(rows))
    elif action == "balance":
        await _edit(cb, await commands.balance(api, uid), back_kb("balance"))
    elif action == "positions":
        text, rows = await commands.positions(api, uid)
        await _edit(cb, text, positions_kb(rows) if rows is not None else back_kb("positions"))
    elif action == "orders":
        await _edit(cb, await commands.orders(api, uid), orders_kb())
    elif action == "closeall_ask":
        await _edit(cb, "❌ <b>Close ALL positions</b> at market price?\nThis flattens every "
                        "open position on the venue.", bulk_confirm_kb("closeall_do"))
    elif action == "cancelall_ask":
        await _edit(cb, "🧹 <b>Cancel ALL orders?</b>\nThis also <b>stops every running grid</b> "
                        "(so they don't re-place orders).", bulk_confirm_kb("cancelall_do"))
    elif action == "panic_ask":
        await _edit(cb, "🛑 <b>Close EVERYTHING?</b>\nStops all grids, cancels all orders, and "
                        "flattens all positions. Use to get fully flat & out.", bulk_confirm_kb("panic_do"))
    elif action in ("closeall_do", "cancelall_do"):
        note = (await commands.close_all_positions(api, uid) if action == "closeall_do"
                else await commands.cancel_all_orders(api, uid))
        text, rows = await commands.positions(api, uid)
        await _edit(cb, note + "\n\n" + text, positions_kb(rows) if rows is not None else back_kb("positions"))
    elif action == "panic_do":
        note = await commands.panic(api, uid)
        await _edit(cb, note, main_menu_kb(await _connected(api, uid)))
    elif action == "price":
        await _edit(cb, "💱 <b>Price</b> — pick a market:", price_kb(settings.market_list()))
    elif action == "wallet":
        await _edit(cb, await commands.wallet(api, uid), back_kb("wallet"))
    elif action == "topup":
        await _edit(cb, await commands.topup(api, uid), back_kb("topup"))
    elif action == "build":  # ⬅️ Back from a value picker → the Set-it-myself builder
        text, kb = await _build_screen(api, uid, state)
        await _edit(cb, text, kb)
    elif action == "help":
        await _edit(cb, commands.HELP, back_kb())


# ── settings cycle / type ────────────────────────────────────────────────────

@router.callback_query(SetCB.filter(F.kind == "open"))
async def cb_setting_open(cb: CallbackQuery, api: WorkerAPI, callback_data: SetCB) -> None:
    await cb.answer()
    s = (await api.get_settings(_uid(cb))).get("settings", {})
    key = callback_data.key
    cur = s.get(key, "")
    help_ = KNOB_HELP.get(key, "")
    body = f"⚙️ <b>{KNOB_LABEL.get(key, key)}</b> — now: <b>{cur}</b>"
    if help_:
        body += f"\n{help_}"
    body += "\n\nTap a value:"
    await _edit(cb, body, setting_picker_kb(key, cur))


@router.callback_query(SetCB.filter(F.kind == "set"))
async def cb_setting_set(cb: CallbackQuery, api: WorkerAPI, state: FSMContext, callback_data: SetCB) -> None:
    toast, new = await commands.set_setting(api, _uid(cb), callback_data.key, callback_data.val)
    await cb.answer(toast, show_alert=new is None)   # alert only on error
    if new is not None:
        text, kb = await _build_screen(api, _uid(cb), state)
        await _edit(cb, text, kb)


@router.callback_query(SetCB.filter(F.kind == "custom"))
async def cb_setting_custom(cb: CallbackQuery, state: FSMContext, callback_data: SetCB) -> None:
    """No alert, no command to memorize: ask for the value and capture the reply."""
    key = callback_data.key
    label = KNOB_LABEL.get(key, key)
    await cb.answer()
    await state.set_state(Configure.value)
    await state.update_data(config_key=key)
    if isinstance(cb.message, Message):
        await cb.message.answer(
            f"✏️ Send the new value for <b>{label}</b> (just the number, e.g. <code>0.5</code>).\n"
            f"/cancel to abort.",
            reply_markup=ForceReply(input_field_placeholder=f"new {label} value"))


@router.message(Configure.value)
async def on_config_value(m: Message, api: WorkerAPI, state: FSMContext) -> None:
    key = (await state.get_data()).get("config_key")
    if not key:
        await state.clear()
        return
    label = KNOB_LABEL.get(key, key)
    toast, new = await commands.set_setting(api, _uid(m), key, (m.text or "").strip())
    if new is None:  # validation failed — stay in the state and re-ask
        await m.answer(f"{toast}\nTry again, or /cancel.",
                       reply_markup=ForceReply(input_field_placeholder=f"new {label} value"))
        return
    await state.set_state(GridWizard.build)
    text, kb = await _build_screen(api, _uid(m), state)
    sent = await m.answer(f"✅ {label} set.\n\n" + text, reply_markup=kb)
    await state.update_data(mid=sent.message_id, chat=sent.chat.id)


# ── price picker ─────────────────────────────────────────────────────────────

@router.callback_query(PosCB.filter(F.action == "close_ask"))
async def cb_pos_close_ask(cb: CallbackQuery, callback_data: PosCB) -> None:
    await cb.answer()
    await _edit(cb, f"❌ Close your <b>{callback_data.market}</b> position at market price?",
                pos_confirm_kb(callback_data.market))


async def _send_card(cb: CallbackQuery, data: dict | None) -> None:
    """Render + send a PnL card photo; a render error never blocks the action."""
    if data is None or not isinstance(cb.message, Message):
        return
    with contextlib.suppress(Exception):
        await cb.message.answer_photo(
            BufferedInputFile(pnlcard.render(data), filename="pnl.png"),
            caption=commands.pnl_caption(data))


@router.callback_query(PosCB.filter(F.action == "close_do"))
async def cb_pos_close_do(cb: CallbackQuery, api: WorkerAPI, callback_data: PosCB) -> None:
    note, card = await commands.close_with_card(api, _uid(cb), callback_data.market)
    await cb.answer(note[:180])
    await _send_card(cb, card)
    text, rows = await commands.positions(api, _uid(cb))
    await _edit(cb, text, positions_kb(rows) if rows is not None else back_kb("positions"))


@router.callback_query(PosCB.filter(F.action == "share"))
async def cb_pos_share(cb: CallbackQuery, api: WorkerAPI, callback_data: PosCB) -> None:
    """📸 — share the LIVE (unrealized) PnL of an open position, no close needed."""
    await cb.answer("📸 PnL card")
    card = None
    with contextlib.suppress(Exception):
        for p in await api.positions(_uid(cb)):
            if p.get("market") == callback_data.market:
                card = commands.position_card(p, live=True)
                break
    await _send_card(cb, card)


@router.callback_query(PriceCB.filter())
async def cb_price_pick(cb: CallbackQuery, api: WorkerAPI, callback_data: PriceCB) -> None:
    await cb.answer()
    text = await commands.price(api, _uid(cb), callback_data.market)
    await _edit(cb, text, price_result_kb(callback_data.market))


# ── per-grid actions (pause / stop) ──────────────────────────────────────────

async def _refresh_grids(cb: CallbackQuery, api: WorkerAPI) -> None:
    rows = await _safe_status(api, _uid(cb))
    await _edit(cb, commands.render_status(rows), grids_kb(rows))


@router.callback_query(GridCB.filter(F.action == "detail"))
async def cb_detail(cb: CallbackQuery, api: WorkerAPI, callback_data: GridCB) -> None:
    await cb.answer()
    text, d = await commands.detail(api, _uid(cb), callback_data.iid)
    if d is None:
        await _edit(cb, text, back_kb("grids"))
        return
    await _edit(cb, text, detail_kb(callback_data.iid, d.get("state") == "RUNNING"))


@router.callback_query(GridCB.filter(F.action == "rename"))
async def cb_rename(cb: CallbackQuery, state: FSMContext, callback_data: GridCB) -> None:
    await cb.answer()
    await state.set_state(Rename.name)
    await state.update_data(rename_iid=callback_data.iid)
    if isinstance(cb.message, Message):
        await cb.message.answer("✏️ Send a name for this grid (e.g. <code>BTC scalp</code>). /cancel to abort.",
                                reply_markup=ForceReply(input_field_placeholder="grid name"))


@router.message(Rename.name)
async def on_rename(m: Message, api: WorkerAPI, state: FSMContext) -> None:
    iid = (await state.get_data()).get("rename_iid")
    await state.clear()
    if not iid:
        return
    name = (m.text or "").strip()[:40]
    try:
        await api.rename_grid(_uid(m), iid, name)
    except Exception as e:  # noqa: BLE001 — surface any backend/transport failure
        await m.answer(f"⚠️ Could not rename: {e}")
        return
    text, d = await commands.detail(api, _uid(m), iid)
    kb = detail_kb(iid, d.get("state") == "RUNNING") if d else back_kb("grids")
    await m.answer(f"✅ Renamed.\n\n{text}", reply_markup=kb)


@router.callback_query(GridCB.filter(F.action == "edit"))
async def cb_edit(cb: CallbackQuery, api: WorkerAPI, state: FSMContext, callback_data: GridCB) -> None:
    """Edit = stop this grid and relaunch with new settings (engines are immutable)."""
    try:
        d = await api.grid_detail(_uid(cb), callback_data.iid)
        await api.stop(_uid(cb), callback_data.iid)
    except Exception as e:  # noqa: BLE001
        await cb.answer(f"Couldn't edit: {e}"[:180], show_alert=True)
        return
    await cb.answer("Stopped — pick new settings")
    market = d.get("market", "")
    if isinstance(cb.message, Message):
        sent = await cb.message.answer(f"🛠 <b>Edit {market}</b> — stopped. Pick a new style:",
                                       reply_markup=wiz_strategy_kb())
        await state.set_state(GridWizard.strategy)
        await state.update_data(cfg={"market": market}, chat=sent.chat.id, mid=sent.message_id)


@router.callback_query(GridCB.filter(F.action == "pause"))
async def cb_pause(cb: CallbackQuery, api: WorkerAPI, callback_data: GridCB) -> None:
    out = await commands.pause(api, _uid(cb), callback_data.iid)
    await cb.answer("⏸ Paused" if "Paused" in out else out[:180])
    await _refresh_grids(cb, api)


@router.callback_query(GridCB.filter(F.action == "stop_ask"))
async def cb_stop_ask(cb: CallbackQuery, callback_data: GridCB) -> None:
    await cb.answer()
    await _edit(cb, f"🛑 Stop <code>{callback_data.iid}</code>?\n"
                    f"This cancels its orders and closes the grid.",
                stop_confirm_kb(callback_data.iid))


@router.callback_query(GridCB.filter(F.action == "share"))
async def cb_grid_share(cb: CallbackQuery, api: WorkerAPI, callback_data: GridCB) -> None:
    """📸 — share the LIVE total PnL (realized + unrealized) of a running grid."""
    await cb.answer("📸 PnL card")
    card = None
    with contextlib.suppress(Exception):
        d = await api.grid_detail(_uid(cb), callback_data.iid)
        if d:
            card = commands.grid_card(d, live=True)
    await _send_card(cb, card)


@router.callback_query(GridCB.filter(F.action == "stop_do"))
async def cb_stop_do(cb: CallbackQuery, api: WorkerAPI, callback_data: GridCB) -> None:
    card = None
    with contextlib.suppress(Exception):       # snapshot the grid's PnL BEFORE stopping
        d = await api.grid_detail(_uid(cb), callback_data.iid)
        if d:
            card = commands.grid_card(d, live=False)
    out = await commands.stop(api, _uid(cb), callback_data.iid)
    await cb.answer("🛑 Stopped")
    await _send_card(cb, card)
    rows = await _safe_status(api, _uid(cb))
    await _edit(cb, out + "\n\n" + commands.render_status(rows), grids_kb(rows))


# ── grid wizard (single message, edited/advanced in place) ───────────────────

async def _wizard_start(message: Message, settings: BotSettings, state: FSMContext) -> None:
    sent = await message.answer(commands.WIZ_MARKET, reply_markup=wiz_market_kb(settings.market_list()))
    await state.set_state(GridWizard.market)
    await state.update_data(cfg={}, chat=sent.chat.id, mid=sent.message_id)


async def _cfg(state: FSMContext) -> dict:
    return (await state.get_data()).get("cfg", {})


async def _edit_wiz(bot: Bot, state: FSMContext, text: str, kb) -> None:
    data = await state.get_data()
    chat, mid = data.get("chat"), data.get("mid")
    if chat and mid:
        with contextlib.suppress(TelegramBadRequest):
            await bot.edit_message_text(text, chat_id=chat, message_id=mid, reply_markup=kb)


async def _send_step(bot: Bot, state: FSMContext, text: str, kb) -> None:
    """Advance: delete the previous step and send the next, so Telegram plays its
    native delete animation and the chat never piles up."""
    data = await state.get_data()
    chat, old = data.get("chat"), data.get("mid")
    if old:
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id=chat, message_id=old)
    sent = await bot.send_message(chat, text, reply_markup=kb)
    await state.update_data(mid=sent.message_id)


async def _set(state: FSMContext, key: str, value) -> dict:
    cfg = await _cfg(state)
    cfg[key] = value
    await state.update_data(cfg=cfg)
    return cfg


async def _drop(message: Message) -> None:
    with contextlib.suppress(Exception):
        await message.delete()


async def _go_strategy(bot: Bot, state: FSMContext, market: str) -> None:
    await _set(state, "market", market)
    await state.set_state(GridWizard.strategy)
    await _send_step(bot, state, f"Coin: <b>{market}</b> ✓\n\n{commands.WIZ_STRATEGY}",
                     wiz_strategy_kb())


async def _build_screen(api: WorkerAPI, user_id: int, state: FSMContext) -> tuple[str, object]:
    """(text, keyboard) for the Set-it-myself builder: the saved knobs for this grid
    + 🚀 Launch. Tapping a value edits the saved settings, snapshotted at launch."""
    market = (await _cfg(state)).get("market", "")
    s = (await api.get_settings(user_id)).get("settings", {})
    return commands.grid_build_card(market, s), grid_build_kb(s)


async def _go_build(bot: Bot, api: WorkerAPI, state: FSMContext, user_id: int) -> None:
    """Manual path: market is set; open the single-screen knob builder."""
    await state.set_state(GridWizard.build)
    text, kb = await _build_screen(api, user_id, state)
    await _send_step(bot, state, text, kb)


@router.callback_query(GridWizard.market, WizCB.filter(F.field == "market"))
async def wiz_market_pick(cb: CallbackQuery, state: FSMContext, bot: Bot, settings: BotSettings,
                          callback_data: WizCB) -> None:
    await cb.answer()
    if callback_data.value == "custom":
        await _edit_wiz(bot, state, "Type a coin symbol, e.g. <code>BTCUSDT</code>:",
                        wiz_market_kb(settings.market_list()))
        return
    await _go_strategy(bot, state, callback_data.value)


@router.message(GridWizard.market)
async def wiz_market_text(message: Message, state: FSMContext, bot: Bot, settings: BotSettings) -> None:
    await _drop(message)
    try:
        market = parse_market(message.text or "")
    except ValueError as e:
        await _edit_wiz(bot, state, f"⚠️ {e}\n\n{commands.WIZ_MARKET}",
                        wiz_market_kb(settings.market_list()))
        return
    await _go_strategy(bot, state, market)


# Step 2 — strategy: a one-tap preset, or "Set it myself" → the single-screen builder
@router.callback_query(GridWizard.strategy, WizCB.filter(F.field == "preset"))
async def wiz_strategy_pick(cb: CallbackQuery, api: WorkerAPI, state: FSMContext, bot: Bot,
                            callback_data: WizCB) -> None:
    await cb.answer()
    if callback_data.value == "custom":
        await _go_build(bot, api, state, _uid(cb))
        return
    cfg = await _cfg(state)
    cfg["template"] = callback_data.value
    await state.update_data(cfg=cfg)
    await state.set_state(GridWizard.margin)
    await _send_step(bot, state, commands.WIZ_MARGIN, wiz_margin_kb())


async def _preview_confirm(bot: Bot, api: WorkerAPI, state: FSMContext, user_id: int,
                           margin=None, margin_pct=None) -> None:
    """Ask the WORKER to size (it reads the real balance) and show the value to confirm."""
    cfg = await _cfg(state)
    cfg["margin"] = str(margin) if margin is not None else None
    cfg["margin_pct"] = str(margin_pct) if margin_pct is not None else None
    await state.update_data(cfg=cfg)
    try:
        plan = await api.preview_grid(user_id, cfg["market"], cfg["template"],
                                      margin=cfg["margin"], margin_pct=cfg["margin_pct"])
    except Exception as e:  # noqa: BLE001
        await _edit_wiz(bot, state, f"⚠️ {e}\n\n{commands.WIZ_MARGIN}", wiz_margin_kb())
        return
    await state.set_state(GridWizard.confirm)
    await _send_step(bot, state, commands.render_template_preview(plan, cfg["template"]), wiz_confirm_kb())


@router.callback_query(GridWizard.margin, WizCB.filter(F.field == "margin"))
async def wiz_margin_pick(cb: CallbackQuery, api: WorkerAPI, state: FSMContext, bot: Bot,
                          callback_data: WizCB) -> None:
    await cb.answer()
    if callback_data.value == "custom":
        await _edit_wiz(bot, state, "Type the margin to commit, in USDT (e.g. <code>100</code>):",
                        wiz_margin_kb())
        return
    # Preset = a fraction; the worker turns it into USDT from the real free balance.
    await _preview_confirm(bot, api, state, _uid(cb), margin_pct=callback_data.value)


@router.message(GridWizard.margin)
async def wiz_margin_text(message: Message, api: WorkerAPI, state: FSMContext, bot: Bot) -> None:
    from decimal import Decimal, InvalidOperation
    await _drop(message)
    try:
        margin = Decimal((message.text or "").strip())
        if margin <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await _edit_wiz(bot, state, f"⚠️ Enter a positive USDT amount.\n\n{commands.WIZ_MARGIN}",
                        wiz_margin_kb())
        return
    await _preview_confirm(bot, api, state, _uid(message), margin=margin)


# Set-it-myself builder: tap-to-edit knobs (SetCB handlers above) + these footer actions.
@router.callback_query(GridWizard.build, WizCB.filter(F.field == "launch"))
async def wiz_launch(cb: CallbackQuery, api: WorkerAPI, state: FSMContext) -> None:
    market = (await _cfg(state)).get("market", "")
    text, iid = await commands.create_grid_result(api, _uid(cb), market)
    if iid is None:  # validation / connection error — keep the builder open to fix
        await cb.answer("Couldn't launch")
        card, kb = await _build_screen(api, _uid(cb), state)
        await _edit(cb, f"{text}\n\n{card}", kb)
        return
    await cb.answer("Launched")
    await _edit(cb, text, launched_kb(iid))
    await state.clear()


@router.callback_query(GridWizard.build, WizCB.filter(F.field == "back"))
async def wiz_back_to_strategy(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    market = (await _cfg(state)).get("market", "")
    await state.set_state(GridWizard.strategy)
    await _edit(cb, f"Coin: <b>{market}</b> ✓\n\n{commands.WIZ_STRATEGY}", wiz_strategy_kb())


@router.callback_query(GridWizard.build, WizCB.filter(F.field == "defaults"))
async def wiz_defaults(cb: CallbackQuery, api: WorkerAPI, state: FSMContext) -> None:
    await api.reset_settings(_uid(cb))
    await cb.answer("Reset to defaults")
    text, kb = await _build_screen(api, _uid(cb), state)
    await _edit(cb, text, kb)


@router.callback_query(WizCB.filter(F.field == "cancel"))
async def wiz_cancel(cb: CallbackQuery, api: WorkerAPI, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await cb.answer("Cancelled")
    await _delete(cb)
    if isinstance(cb.message, Message):
        await _send_home(cb.message, api, _uid(cb))


@router.callback_query(GridWizard.confirm, WizCB.filter(F.field == "confirm"))
async def wiz_confirm(cb: CallbackQuery, api: WorkerAPI, state: FSMContext, bot: Bot) -> None:
    """Confirm a one-tap template launch (the Set-it-myself path launches from its
    own builder screen)."""
    cfg = await _cfg(state)
    await cb.answer("Launching…")
    text, iid = await commands.create_template_result(
        api, _uid(cb), cfg["market"], cfg["template"], cfg.get("margin"), cfg.get("margin_pct"))
    await _send_step(bot, state, text, launched_kb(iid) if iid else back_kb())
    await state.clear()


# ── runner ───────────────────────────────────────────────────────────────────

async def _run(settings: BotSettings) -> None:
    api = WorkerAPI(settings.api_url)
    bot = Bot(settings.token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(api=api, settings=settings, storage=MemoryStorage())
    allow = Allowlist(settings.allowed_ids())
    router.message.middleware(allow)
    router.callback_query.middleware(allow)
    dp.include_router(router)
    await bot.set_my_commands(_COMMANDS)
    try:
        await dp.start_polling(bot)
    finally:
        await api.close()


def main() -> None:
    settings = BotSettings()
    if not settings.token:
        raise SystemExit("set PERPSBOT_TOKEN in frontend/telegram/.env (token from @BotFather)")
    asyncio.run(_run(settings))


if __name__ == "__main__":
    main()
