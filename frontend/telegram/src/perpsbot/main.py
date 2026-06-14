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
from aiogram.types import BotCommand, CallbackQuery, Message

from . import commands, ui
from .api import WorkerAPI
from .config import BotSettings, is_allowed
from .keyboards import (
    GridCB, MenuCB, PriceCB, SetCB, WizCB,
    back_kb, grids_kb, launched_kb, main_menu_kb, price_kb, price_result_kb,
    settings_kb, stop_confirm_kb, wiz_band_kb, wiz_confirm_kb, wiz_levels_kb,
    wiz_market_kb, wiz_size_kb, wiz_strategy_kb,
)
from .wizard import (
    PRESETS, GridWizard, parse_band_pct, parse_levels, parse_market, parse_size,
)

router = Router()

_COMMANDS = [
    BotCommand(command="start", description="🏠 Home — balance, positions, all buttons"),
    BotCommand(command="grid", description="➕ Launch a grid (wizard or args)"),
    BotCommand(command="status", description="📊 Your grids"),
    BotCommand(command="wallet", description="👛 Your MNT wallet"),
    BotCommand(command="topup", description="💧 Fund your MNT wallet"),
    BotCommand(command="balance", description="💰 Venue equity"),
    BotCommand(command="price", description="💱 Live top-of-book"),
    BotCommand(command="settings", description="⚙️ Strategy settings"),
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


@router.message(Command("wallet"))
async def cmd_wallet(message: Message, api: WorkerAPI) -> None:
    await message.answer(await commands.wallet(api, _uid(message)), reply_markup=back_kb("wallet"))


@router.message(Command("topup"))
async def cmd_topup(message: Message, api: WorkerAPI) -> None:
    await message.answer(await commands.topup(api, _uid(message)), reply_markup=back_kb("topup"))


@router.message(Command("settings"))
async def cmd_settings(message: Message, api: WorkerAPI) -> None:
    resp = await api.get_settings(_uid(message))
    s = resp.get("settings", {})
    await message.answer(commands.settings_card(s, resp.get("customized")), reply_markup=settings_kb(s))


@router.message(Command("set"))
async def cmd_set(message: Message, api: WorkerAPI) -> None:
    await message.answer(await commands.set_value(api, _uid(message), _args(message)))


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
    elif action == "balance":
        await _edit(cb, await commands.balance(api, uid), back_kb("balance"))
    elif action == "price":
        await _edit(cb, "💱 <b>Price</b> — pick a market:", price_kb(settings.market_list()))
    elif action == "wallet":
        await _edit(cb, await commands.wallet(api, uid), back_kb("wallet"))
    elif action == "topup":
        await _edit(cb, await commands.topup(api, uid), back_kb("topup"))
    elif action == "settings":
        resp = await api.get_settings(uid)
        s = resp.get("settings", {})
        await _edit(cb, commands.settings_card(s, resp.get("customized")), settings_kb(s))
    elif action == "reset":
        resp = await api.reset_settings(uid)
        s = resp.get("settings", {})
        await _edit(cb, "↩️ Settings reset.\n\n" + commands.settings_card(s), settings_kb(s))
    elif action == "help":
        await _edit(cb, commands.HELP, back_kb())


# ── settings cycle / type ────────────────────────────────────────────────────

@router.callback_query(SetCB.filter(F.kind == "cycle"))
async def cb_setting_cycle(cb: CallbackQuery, api: WorkerAPI, callback_data: SetCB) -> None:
    toast, new = await commands.cycle_setting(api, _uid(cb), callback_data.key, ui.next_value)
    await cb.answer(toast)
    if new:
        await _edit(cb, commands.settings_card(new), settings_kb(new))


@router.callback_query(SetCB.filter(F.kind == "type"))
async def cb_setting_type(cb: CallbackQuery, callback_data: SetCB) -> None:
    key = callback_data.key
    await cb.answer(f"Type:  /set {key} VALUE\ne.g.  /set {key} 0.5", show_alert=True)


# ── price picker ─────────────────────────────────────────────────────────────

@router.callback_query(PriceCB.filter())
async def cb_price_pick(cb: CallbackQuery, api: WorkerAPI, callback_data: PriceCB) -> None:
    await cb.answer()
    text = await commands.price(api, _uid(cb), callback_data.market)
    await _edit(cb, text, price_result_kb(callback_data.market))


# ── per-grid actions (pause / stop) ──────────────────────────────────────────

async def _refresh_grids(cb: CallbackQuery, api: WorkerAPI) -> None:
    rows = await _safe_status(api, _uid(cb))
    await _edit(cb, commands.render_status(rows), grids_kb(rows))


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


@router.callback_query(GridCB.filter(F.action == "stop_do"))
async def cb_stop_do(cb: CallbackQuery, api: WorkerAPI, callback_data: GridCB) -> None:
    out = await commands.stop(api, _uid(cb), callback_data.iid)
    await cb.answer("🛑 Stopped")
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


async def _go_band(bot: Bot, state: FSMContext) -> None:
    """Manual path: market is already set; ask for the range."""
    await state.set_state(GridWizard.band)
    await _send_step(bot, state, commands.WIZ_BAND, wiz_band_kb())


async def _go_levels(bot: Bot, state: FSMContext, band: str) -> None:
    from decimal import Decimal
    await _set(state, "band", band)
    await state.set_state(GridWizard.levels)
    pct = (Decimal(band) * 100).normalize()
    await _send_step(bot, state, f"Range set to ±{pct}% ✓\n\n{commands.WIZ_LEVELS}", wiz_levels_kb())


async def _go_size(bot: Bot, state: FSMContext, levels: int) -> None:
    await _set(state, "levels", levels)
    await state.set_state(GridWizard.size)
    await _send_step(bot, state, f"Steps: {levels} ✓\n\n{commands.WIZ_SIZE}", wiz_size_kb())


async def _go_confirm(bot: Bot, state: FSMContext, size: str) -> None:
    cfg = await _set(state, "size", size)
    await state.set_state(GridWizard.confirm)
    await _send_step(bot, state, commands.grid_confirm_text(
        cfg["market"], cfg["band"], cfg["levels"], cfg["size"]), wiz_confirm_kb())


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


# Step 2 — strategy: a one-tap preset, or "Set it myself" → manual range/steps/size
@router.callback_query(GridWizard.strategy, WizCB.filter(F.field == "preset"))
async def wiz_strategy_pick(cb: CallbackQuery, state: FSMContext, bot: Bot,
                            callback_data: WizCB) -> None:
    await cb.answer()
    if callback_data.value == "custom":
        await _go_band(bot, state)
        return
    preset = PRESETS[callback_data.value]
    cfg = await _cfg(state)
    cfg.update(band=preset["band"], levels=preset["levels"], size=preset["size"])
    await state.update_data(cfg=cfg)
    await state.set_state(GridWizard.confirm)
    await _send_step(bot, state, commands.grid_confirm_text(
        cfg["market"], cfg["band"], cfg["levels"], cfg["size"], style=callback_data.value),
        wiz_confirm_kb())


@router.callback_query(GridWizard.band, WizCB.filter(F.field == "band"))
async def wiz_band_pick(cb: CallbackQuery, state: FSMContext, bot: Bot, callback_data: WizCB) -> None:
    await cb.answer()
    if callback_data.value == "custom":
        await _edit_wiz(bot, state, "Type the range in %, e.g. <code>1</code> for ±1%:", wiz_band_kb())
        return
    await _go_levels(bot, state, parse_band_pct(callback_data.value))


@router.message(GridWizard.band)
async def wiz_band_text(message: Message, state: FSMContext, bot: Bot) -> None:
    await _drop(message)
    try:
        band = parse_band_pct(message.text or "")
    except ValueError as e:
        await _edit_wiz(bot, state, f"⚠️ {e}\n\n{commands.WIZ_BAND}", wiz_band_kb())
        return
    await _go_levels(bot, state, band)


@router.callback_query(GridWizard.levels, WizCB.filter(F.field == "levels"))
async def wiz_levels_pick(cb: CallbackQuery, state: FSMContext, bot: Bot, callback_data: WizCB) -> None:
    await cb.answer()
    if callback_data.value == "custom":
        await _edit_wiz(bot, state, "Type how many steps (2–200):", wiz_levels_kb())
        return
    await _go_size(bot, state, parse_levels(callback_data.value))


@router.message(GridWizard.levels)
async def wiz_levels_text(message: Message, state: FSMContext, bot: Bot) -> None:
    await _drop(message)
    try:
        levels = parse_levels(message.text or "")
    except ValueError as e:
        await _edit_wiz(bot, state, f"⚠️ {e}\n\n{commands.WIZ_LEVELS}", wiz_levels_kb())
        return
    await _go_size(bot, state, levels)


@router.callback_query(GridWizard.size, WizCB.filter(F.field == "size"))
async def wiz_size_pick(cb: CallbackQuery, state: FSMContext, bot: Bot, callback_data: WizCB) -> None:
    await cb.answer()
    if callback_data.value == "custom":
        await _edit_wiz(bot, state, "Type the size per step, e.g. <code>0.001</code>:", wiz_size_kb())
        return
    await _go_confirm(bot, state, parse_size(callback_data.value))


@router.message(GridWizard.size)
async def wiz_size_text(message: Message, state: FSMContext, bot: Bot) -> None:
    await _drop(message)
    try:
        size = parse_size(message.text or "")
    except ValueError as e:
        await _edit_wiz(bot, state, f"⚠️ {e}\n\n{commands.WIZ_SIZE}", wiz_size_kb())
        return
    await _go_confirm(bot, state, size)


@router.callback_query(WizCB.filter(F.field == "cancel"))
async def wiz_cancel(cb: CallbackQuery, api: WorkerAPI, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await cb.answer("Cancelled")
    await _delete(cb)
    if isinstance(cb.message, Message):
        await _send_home(cb.message, api, _uid(cb))


@router.callback_query(GridWizard.confirm, WizCB.filter(F.field == "confirm"))
async def wiz_confirm(cb: CallbackQuery, api: WorkerAPI, state: FSMContext, bot: Bot) -> None:
    cfg = await _cfg(state)
    await cb.answer("Launching…")
    text, iid = await commands.create_grid_result(
        api, _uid(cb), cfg["market"], cfg["band"], cfg["levels"], cfg["size"])
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
