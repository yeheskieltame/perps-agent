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
from aiogram.types import Message

from . import commands
from .api import WorkerAPI
from .config import BotSettings, is_allowed

router = Router()


def _args(m: Message) -> str:
    return (m.text or "").partition(" ")[2].strip()


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


@router.message(Command("grid"))
async def on_grid(m: Message, api: WorkerAPI, settings: BotSettings) -> None:
    await m.answer(await commands.grid(api, m.from_user.id, _args(m),
                                       band=settings.default_band,
                                       levels=settings.default_levels,
                                       size=settings.default_size))


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
