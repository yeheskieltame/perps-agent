"""Entrypoint — aiogram v3 wiring. Handlers are one-liners over `commands` (pure,
tested offline); this module owns only Telegram plumbing + the allowlist.

    cd frontend/telegram && cp .env.example .env   # fill PERPSBOT_TOKEN
    python -m perpsbot.main
"""
from __future__ import annotations

import asyncio

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from . import commands, ui
from .api import WorkerAPI
from .config import BotSettings, is_allowed

router = Router()


def _args(m: Message) -> str:
    return (m.text or "").partition(" ")[2].strip()


class Connect(StatesGroup):
    """The /connect dialog: collect key → secret → environment, then store."""

    key = State()
    secret = State()
    env = State()


async def _scrub(m: Message) -> str:
    """Delete a message that carries a secret. Best-effort: if Telegram refuses,
    tell the user to wipe it themselves rather than failing silently."""
    try:
        await m.delete()
        return ""
    except Exception:
        return "\n⚠️ I could not delete your last message — please delete it manually."


class Allowlist(BaseMiddleware):
    """Drop messages from senders outside PERPSBOT_ALLOWLIST (empty = open/dev)."""

    def __init__(self, allow: set[int]) -> None:
        self.allow = allow

    async def __call__(self, handler, event: Message, data):
        uid = event.from_user.id if event.from_user else None
        if not is_allowed(uid, self.allow):
            return await event.answer("⛔ Access denied.")
        return await handler(event, data)


# ---- one-screen dashboard (ui.py builds rows; everything edits in place) ----

def _kb(rows: ui.Rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


async def _dash(api: WorkerAPI, user_id: int, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    creds, balance, grids = await commands.dashboard_data(api, user_id)
    text = ui.dashboard_text(creds, balance, grids, note)
    return text, _kb(ui.menu_rows(bool(creds and creds.get("connected"))))


async def _edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    """Edit the dashboard message in place; 'message is not modified' is fine."""
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


async def _show_home(cb: CallbackQuery, api: WorkerAPI, note: str = "") -> None:
    text, kb = await _dash(api, cb.from_user.id, note)
    await _edit(cb, text, kb)


async def _show_settings(cb: CallbackQuery, api: WorkerAPI) -> None:
    resp = await api.get_settings(cb.from_user.id)
    settings = resp.get("settings", {})
    text = commands.settings_card(settings, resp.get("customized"))
    await _edit(cb, text, _kb(ui.settings_rows(settings)))


@router.message(CommandStart())
async def on_start(m: Message, api: WorkerAPI) -> None:
    text, kb = await _dash(api, m.from_user.id)
    await m.answer(text, reply_markup=kb)


@router.message(Command("help"))
async def on_help(m: Message) -> None:
    await m.answer(commands.HELP)


@router.callback_query(F.data.startswith("d:"))
async def on_nav(cb: CallbackQuery, api: WorkerAPI, state: FSMContext,
                 settings: BotSettings) -> None:
    action = cb.data[2:]
    uid = cb.from_user.id
    if action == "home":
        await _show_home(cb, api)
    elif action == "launch":
        await _edit(cb, "🚀 <b>Pick a market</b> — launches with your /settings\n"
                        "Other market or one-off tweak: <code>/grid MARKET KEY=VALUE</code>",
                    _kb(ui.market_rows(settings.market_list(), "g")))
    elif action == "price":
        await _edit(cb, "💱 <b>Pick a market</b>",
                    _kb(ui.market_rows(settings.market_list(), "p")))
    elif action == "stopmenu":
        _, _, grids = await commands.dashboard_data(api, uid)
        if not grids:
            await cb.answer("No grids running.")
            return
        await _edit(cb, "⏹ <b>Stop which grid?</b>",
                    _kb(ui.stop_rows([g["instance_id"] for g in grids])))
    elif action == "settings":
        await _show_settings(cb, api)
    elif action == "reset":
        await api.reset_settings(uid)
        await _show_settings(cb, api)
        await cb.answer("Settings reset to defaults.")
        return
    elif action == "help":
        await _edit(cb, commands.HELP, _kb(ui.back_rows()))
    elif action == "connect":
        if cb.message.chat.type != "private":
            await cb.answer("🔒 DM only — open a private chat with me.", show_alert=True)
            return
        await state.set_state(Connect.key)
        await cb.message.answer(await commands.connect_start(api, uid))
    elif action == "disconnect":
        await _show_home(cb, api, await commands.disconnect(api, uid))
    await cb.answer()


@router.callback_query(F.data.startswith("g:"))
async def on_launch_market(cb: CallbackQuery, api: WorkerAPI) -> None:
    await _show_home(cb, api, await commands.launch_note(api, cb.from_user.id, cb.data[2:]))
    await cb.answer()


@router.callback_query(F.data.startswith("p:"))
async def on_price_pick(cb: CallbackQuery, api: WorkerAPI) -> None:
    await cb.answer(await commands.price_toast(api, cb.from_user.id, cb.data[2:]),
                    show_alert=True)


@router.callback_query(F.data.startswith("x:"))
async def on_stop_pick(cb: CallbackQuery, api: WorkerAPI) -> None:
    await _show_home(cb, api, await commands.stop_note(api, cb.from_user.id, cb.data[2:]))
    await cb.answer()


@router.callback_query(F.data.startswith("s:"))
async def on_cycle_setting(cb: CallbackQuery, api: WorkerAPI) -> None:
    toast, new = await commands.cycle_setting(api, cb.from_user.id, cb.data[2:], ui.next_value)
    await cb.answer(toast)
    if new:
        await _edit(cb, commands.settings_card(new), _kb(ui.settings_rows(new)))


@router.callback_query(F.data.startswith("t:"))
async def on_typed_setting(cb: CallbackQuery) -> None:
    key = cb.data[2:]
    await cb.answer(f"Type:  /set {key} VALUE\ne.g.  /set {key} 0.5", show_alert=True)


@router.message(Command("cancel"))
async def on_cancel(m: Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer(commands.CONNECT_CANCELLED)


@router.message(Command("connect"))
async def on_connect(m: Message, state: FSMContext, api: WorkerAPI) -> None:
    if m.chat.type != "private":
        await m.answer(commands.CONNECT_DM_ONLY)
        return
    await state.set_state(Connect.key)
    await m.answer(await commands.connect_start(api, m.from_user.id))


@router.message(Command("disconnect"))
async def on_disconnect(m: Message, api: WorkerAPI) -> None:
    await m.answer(await commands.disconnect(api, m.from_user.id))


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
    await m.answer(await commands.connect_finish(
        api, m.from_user.id, data["key"], data["secret"], testnet=choice))
    text, kb = await _dash(api, m.from_user.id)   # land back on the dashboard
    await m.answer(text, reply_markup=kb)


@router.message(Command("grid"))
async def on_grid(m: Message, api: WorkerAPI) -> None:
    await m.answer(await commands.grid(api, m.from_user.id, _args(m)))


@router.message(Command("settings"))
async def on_settings(m: Message, api: WorkerAPI) -> None:
    await m.answer(await commands.settings_show(api, m.from_user.id))


@router.message(Command("set"))
async def on_set(m: Message, api: WorkerAPI) -> None:
    await m.answer(await commands.set_value(api, m.from_user.id, _args(m)))


@router.message(Command("status"))
async def on_status(m: Message, api: WorkerAPI) -> None:
    await m.answer(await commands.status(api, m.from_user.id))


@router.message(Command("stop"))
async def on_stop(m: Message, api: WorkerAPI) -> None:
    await m.answer(await commands.stop(api, m.from_user.id, _args(m)))


@router.message(Command("pause"))
async def on_pause(m: Message, api: WorkerAPI) -> None:
    await m.answer(await commands.pause(api, m.from_user.id, _args(m)))


@router.message(Command("price"))
async def on_price(m: Message, api: WorkerAPI) -> None:
    await m.answer(await commands.price(api, m.from_user.id, _args(m)))


@router.message(Command("balance"))
async def on_balance(m: Message, api: WorkerAPI) -> None:
    await m.answer(await commands.balance(api, m.from_user.id))


@router.message(Command("health"))
async def on_health(m: Message, api: WorkerAPI) -> None:
    await m.answer(await commands.health(api))


COMMAND_MENU = [
    ("start", "dashboard — everything on one screen"),
    ("grid", "launch: /grid MARKET [KEY=VALUE ...]"),
    ("settings", "your strategy settings"),
    ("set", "change one: /set KEY VALUE"),
    ("status", "your grids"),
    ("stop", "stop a grid"),
    ("balance", "venue equity"),
    ("price", "top-of-book"),
    ("connect", "link Bybit API keys (DM)"),
    ("help", "all commands"),
]


async def _run(settings: BotSettings) -> None:
    api = WorkerAPI(settings.api_url)
    bot = Bot(settings.token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(api=api, settings=settings)
    allow = Allowlist(settings.allowed_ids())
    router.message.middleware(allow)
    router.callback_query.middleware(allow)  # buttons must pass the same gate
    dp.include_router(router)
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMAND_MENU])
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
