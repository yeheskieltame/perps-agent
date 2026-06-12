"""Entrypoint — aiogram v3 wiring. Handlers are one-liners over `commands` (pure,
tested offline); this module owns only Telegram plumbing + the allowlist.

    cd frontend/telegram && cp .env.example .env   # fill PERPSBOT_TOKEN
    python -m perpsbot.main
"""
from __future__ import annotations

import asyncio

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from . import commands
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


@router.message(CommandStart())
@router.message(Command("help"))
async def on_start(m: Message) -> None:
    await m.answer(commands.HELP)


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


async def _run(settings: BotSettings) -> None:
    api = WorkerAPI(settings.api_url)
    bot = Bot(settings.token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(api=api, settings=settings)
    router.message.middleware(Allowlist(settings.allowed_ids()))
    dp.include_router(router)
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
