"""Entrypoint — aiogram v3 wiring. The typed commands and the inline-keyboard UI
both delegate to the pure renderers in `commands`, so they stay in lockstep; this
module owns only the Telegram plumbing (menu, wizard FSM, callbacks, allowlist).

    cd frontend/telegram && cp .env.example .env   # fill PERPSBOT_TOKEN
    python -m perpsbot.main
"""
from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, MenuButtonCommands, Message

from . import commands
from .api import WorkerAPI
from .config import BotSettings, is_allowed
from .keyboards import (
    GridCB, MenuCB, PriceCB, WizCB,
    back_kb, grids_kb, launched_kb, main_menu_kb, price_kb, price_result_kb,
    stop_confirm_kb, wiz_band_kb, wiz_confirm_kb, wiz_levels_kb, wiz_market_kb,
    wiz_size_kb,
)
from .wizard import GridWizard, parse_band_pct, parse_levels, parse_market, parse_size

router = Router()

_COMMANDS = [
    BotCommand(command="start", description="🏠 Main menu"),
    BotCommand(command="menu", description="🏠 Show the main menu"),
    BotCommand(command="grid", description="➕ Launch a grid (wizard or args)"),
    BotCommand(command="status", description="📊 Your grids"),
    BotCommand(command="price", description="💱 Live top-of-book"),
    BotCommand(command="balance", description="💰 Venue equity"),
    BotCommand(command="help", description="ℹ️ Help"),
]


def _args(m: Message) -> str:
    return (m.text or "").partition(" ")[2].strip()


def _uid(event: Message | CallbackQuery) -> int:
    assert event.from_user is not None
    return event.from_user.id


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


# ── menu helpers ─────────────────────────────────────────────────────────────

async def _show_menu(message: Message, api: WorkerAPI, user_id: int) -> None:
    await message.answer(await commands.menu_snapshot(api, user_id),
                         reply_markup=main_menu_kb())


async def _show_grids(message: Message, api: WorkerAPI, user_id: int,
                      *, hint: str | None = None) -> None:
    try:
        rows = await api.status(user_id)
    except Exception as e:  # noqa: BLE001 — surface any backend/transport failure
        await message.answer(f"⚠️ {e}", reply_markup=back_kb())
        return
    text = commands.render_status(rows)
    if hint:
        text += f"\n\n<i>{hint}</i>"
    await message.answer(text, reply_markup=grids_kb(rows))


# ── commands ─────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, api: WorkerAPI, state: FSMContext) -> None:
    await state.clear()
    await _show_menu(message, api, _uid(message))


@router.message(Command("menu"))
async def cmd_menu(message: Message, api: WorkerAPI, state: FSMContext) -> None:
    await state.clear()
    await _show_menu(message, api, _uid(message))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(commands.HELP, reply_markup=back_kb())


@router.message(Command("grid"))
async def cmd_grid(message: Message, api: WorkerAPI, settings: BotSettings,
                   state: FSMContext) -> None:
    args = _args(message)
    if args:  # power path: /grid BTCUSDT 0.8 12 0.002 still works
        await message.answer(await commands.grid(
            api, _uid(message), args, band=settings.default_band,
            levels=settings.default_levels, size=settings.default_size))
    else:  # no args -> open the step-by-step wizard
        await _wizard_start(message, state)


@router.message(Command("status"))
async def cmd_status(message: Message, api: WorkerAPI) -> None:
    await _show_grids(message, api, _uid(message))


@router.message(Command("stop"))
async def cmd_stop(message: Message, api: WorkerAPI) -> None:
    args = _args(message)
    if args:
        await message.answer(await commands.stop(api, _uid(message), args))
    else:
        await _show_grids(message, api, _uid(message), hint="Tap a grid's 🛑 to stop it.")


@router.message(Command("pause"))
async def cmd_pause(message: Message, api: WorkerAPI) -> None:
    args = _args(message)
    if args:
        await message.answer(await commands.pause(api, _uid(message), args))
    else:
        await _show_grids(message, api, _uid(message), hint="Tap a grid's ⏸ to pause it.")


@router.message(Command("price"))
async def cmd_price(message: Message, api: WorkerAPI) -> None:
    args = _args(message)
    if args:
        market = args.split()[0].upper()
        await message.answer(await commands.price(api, _uid(message), market),
                             reply_markup=price_result_kb(market))
    else:
        await message.answer("💱 <b>Price</b> — pick a market:", reply_markup=price_kb())


@router.message(Command("balance"))
async def cmd_balance(message: Message, api: WorkerAPI) -> None:
    await message.answer(await commands.balance(api, _uid(message)),
                         reply_markup=back_kb("balance"))


# ── main-menu callbacks ──────────────────────────────────────────────────────

@router.callback_query(MenuCB.filter(F.action == "refresh"))
async def cb_refresh(cb: CallbackQuery, api: WorkerAPI) -> None:
    await cb.answer("Refreshed")
    if isinstance(cb.message, Message):
        with contextlib.suppress(TelegramBadRequest):  # ignore "message is not modified"
            await cb.message.edit_text(await commands.menu_snapshot(api, _uid(cb)),
                                       reply_markup=main_menu_kb())


@router.callback_query(MenuCB.filter(F.action == "back"))
async def cb_back(cb: CallbackQuery, api: WorkerAPI, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    if isinstance(cb.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.delete()
        await _show_menu(cb.message, api, _uid(cb))


@router.callback_query(MenuCB.filter(F.action == "close"))
async def cb_close(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    if isinstance(cb.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.delete()


@router.callback_query(MenuCB.filter(F.action == "help"))
async def cb_help(cb: CallbackQuery) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.answer(commands.HELP, reply_markup=back_kb())


@router.callback_query(MenuCB.filter(F.action == "grids"))
async def cb_grids(cb: CallbackQuery, api: WorkerAPI) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await _show_grids(cb.message, api, _uid(cb))


@router.callback_query(MenuCB.filter(F.action == "balance"))
async def cb_balance(cb: CallbackQuery, api: WorkerAPI) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.answer(await commands.balance(api, _uid(cb)),
                                reply_markup=back_kb("balance"))


@router.callback_query(MenuCB.filter(F.action == "price"))
async def cb_price(cb: CallbackQuery) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.answer("💱 <b>Price</b> — pick a market:", reply_markup=price_kb())


@router.callback_query(MenuCB.filter(F.action == "new_grid"))
async def cb_new_grid(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await _wizard_start(cb.message, state)


# ── price picker ─────────────────────────────────────────────────────────────

@router.callback_query(PriceCB.filter())
async def cb_price_pick(cb: CallbackQuery, api: WorkerAPI, callback_data: PriceCB) -> None:
    await cb.answer()
    market = callback_data.market
    text = await commands.price(api, _uid(cb), market)
    if isinstance(cb.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.edit_text(text, reply_markup=price_result_kb(market))


# ── per-grid actions (pause / stop) ──────────────────────────────────────────

async def _refresh_grids(cb: CallbackQuery, api: WorkerAPI) -> None:
    rows: list[dict] = []
    with contextlib.suppress(Exception):
        rows = await api.status(_uid(cb))
    if isinstance(cb.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.edit_text(commands.render_status(rows), reply_markup=grids_kb(rows))


@router.callback_query(GridCB.filter(F.action == "pause"))
async def cb_pause(cb: CallbackQuery, api: WorkerAPI, callback_data: GridCB) -> None:
    out = await commands.pause(api, _uid(cb), callback_data.iid)
    await cb.answer("⏸ Paused" if "Paused" in out else out[:180])
    await _refresh_grids(cb, api)


@router.callback_query(GridCB.filter(F.action == "stop_ask"))
async def cb_stop_ask(cb: CallbackQuery, callback_data: GridCB) -> None:
    await cb.answer()
    iid = callback_data.iid
    if isinstance(cb.message, Message):
        await cb.message.answer(
            f"🛑 Stop <code>{iid}</code>?\nThis cancels its orders and closes the grid.",
            reply_markup=stop_confirm_kb(iid))


@router.callback_query(GridCB.filter(F.action == "stop_do"))
async def cb_stop_do(cb: CallbackQuery, api: WorkerAPI, callback_data: GridCB) -> None:
    out = await commands.stop(api, _uid(cb), callback_data.iid)
    await cb.answer("🛑 Stopped")
    if isinstance(cb.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.edit_text(out)
        await _show_grids(cb.message, api, _uid(cb))


# ── grid wizard ──────────────────────────────────────────────────────────────
# The wizard lives in ONE message that is edited in place as steps advance, so the
# steps never pile up. Preset taps (callbacks) edit it directly. A typed custom
# value also edits it; the user's own input bubble can't be removed in a private
# chat (Telegram forbids a bot deleting a user's message there), but the wizard
# itself stays a single bubble.

async def _wizard_start(message: Message, state: FSMContext) -> None:
    sent = await message.answer(
        "➕ <b>New grid</b> · Step 1/4\nPick a market (or type one):",
        reply_markup=wiz_market_kb())
    await state.set_state(GridWizard.market)
    await state.update_data(cfg={}, chat=sent.chat.id, mid=sent.message_id)


async def _cfg(state: FSMContext) -> dict:
    return (await state.get_data()).get("cfg", {})


async def _edit_wiz(bot: Bot, state: FSMContext, text: str, kb) -> None:
    """Edit the single wizard message in place — no new bubble."""
    data = await state.get_data()
    chat, mid = data.get("chat"), data.get("mid")
    if chat and mid:
        with contextlib.suppress(TelegramBadRequest):
            await bot.edit_message_text(text, chat_id=chat, message_id=mid, reply_markup=kb)


async def _send_step(bot: Bot, state: FSMContext, text: str, kb) -> None:
    """Advance to the next step as a NEW message and DELETE the previous one, so
    Telegram plays its native delete (poof) animation on the old input bubble. The
    chat never piles up, and each transition reads as a real delete-then-show."""
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


async def _go_band(bot: Bot, state: FSMContext, market: str) -> None:
    await _set(state, "market", market)
    await state.set_state(GridWizard.band)
    await _send_step(bot, state, f"Market: <b>{market}</b> ✓\nStep 2/4 — band %:",
                        wiz_band_kb())


async def _go_levels(bot: Bot, state: FSMContext, band: str) -> None:
    await _set(state, "band", band)
    await state.set_state(GridWizard.levels)
    pct = (Decimal(band) * 100).normalize()
    await _send_step(bot, state, f"Band: <b>±{pct}%</b> ✓\nStep 3/4 — levels:",
                        wiz_levels_kb())


async def _go_size(bot: Bot, state: FSMContext, levels: int) -> None:
    await _set(state, "levels", levels)
    await state.set_state(GridWizard.size)
    await _send_step(bot, state, f"Levels: <b>{levels}</b> ✓\nStep 4/4 — size per level:",
                        wiz_size_kb())


async def _go_confirm(bot: Bot, state: FSMContext, size: str) -> None:
    cfg = await _set(state, "size", size)
    await state.set_state(GridWizard.confirm)
    await _send_step(
        bot, state,
        commands.grid_confirm_text(cfg["market"], cfg["band"], cfg["levels"], cfg["size"]),
        wiz_confirm_kb())


async def _drop(message: Message) -> None:
    """Best-effort delete of the user's typed value (works in groups; a no-op in
    private chats, where Telegram forbids a bot deleting a user's message)."""
    with contextlib.suppress(Exception):
        await message.delete()


# Step 1 — market
@router.callback_query(GridWizard.market, WizCB.filter(F.field == "market"))
async def wiz_market_pick(cb: CallbackQuery, state: FSMContext, bot: Bot,
                          callback_data: WizCB) -> None:
    await cb.answer()
    if callback_data.value == "custom":
        await _edit_wiz(bot, state,
                        "Step 1/4 — type a market symbol, e.g. <code>BTCUSDT</code>:",
                        wiz_market_kb())
        return
    await _go_band(bot, state, callback_data.value)


@router.message(GridWizard.market)
async def wiz_market_text(message: Message, state: FSMContext, bot: Bot) -> None:
    await _drop(message)
    try:
        market = parse_market(message.text or "")
    except ValueError as e:
        await _edit_wiz(bot, state, f"⚠️ {e}\nStep 1/4 — pick a market (or type one):",
                        wiz_market_kb())
        return
    await _go_band(bot, state, market)


# Step 2 — band
@router.callback_query(GridWizard.band, WizCB.filter(F.field == "band"))
async def wiz_band_pick(cb: CallbackQuery, state: FSMContext, bot: Bot,
                        callback_data: WizCB) -> None:
    await cb.answer()
    if callback_data.value == "custom":
        await _edit_wiz(bot, state, "Step 2/4 — type band in percent, e.g. <code>1</code> for ±1%:",
                        wiz_band_kb())
        return
    await _go_levels(bot, state, parse_band_pct(callback_data.value))  # preset always valid


@router.message(GridWizard.band)
async def wiz_band_text(message: Message, state: FSMContext, bot: Bot) -> None:
    await _drop(message)
    try:
        band = parse_band_pct(message.text or "")
    except ValueError as e:
        await _edit_wiz(bot, state, f"⚠️ {e}\nStep 2/4 — band %:", wiz_band_kb())
        return
    await _go_levels(bot, state, band)


# Step 3 — levels
@router.callback_query(GridWizard.levels, WizCB.filter(F.field == "levels"))
async def wiz_levels_pick(cb: CallbackQuery, state: FSMContext, bot: Bot,
                          callback_data: WizCB) -> None:
    await cb.answer()
    if callback_data.value == "custom":
        await _edit_wiz(bot, state, "Step 3/4 — type the number of levels (2–200):",
                        wiz_levels_kb())
        return
    await _go_size(bot, state, parse_levels(callback_data.value))


@router.message(GridWizard.levels)
async def wiz_levels_text(message: Message, state: FSMContext, bot: Bot) -> None:
    await _drop(message)
    try:
        levels = parse_levels(message.text or "")
    except ValueError as e:
        await _edit_wiz(bot, state, f"⚠️ {e}\nStep 3/4 — levels:", wiz_levels_kb())
        return
    await _go_size(bot, state, levels)


# Step 4 — size
@router.callback_query(GridWizard.size, WizCB.filter(F.field == "size"))
async def wiz_size_pick(cb: CallbackQuery, state: FSMContext, bot: Bot,
                        callback_data: WizCB) -> None:
    await cb.answer()
    if callback_data.value == "custom":
        await _edit_wiz(bot, state,
                        "Step 4/4 — type the base size per level, e.g. <code>0.001</code>:",
                        wiz_size_kb())
        return
    await _go_confirm(bot, state, parse_size(callback_data.value))


@router.message(GridWizard.size)
async def wiz_size_text(message: Message, state: FSMContext, bot: Bot) -> None:
    await _drop(message)
    try:
        size = parse_size(message.text or "")
    except ValueError as e:
        await _edit_wiz(bot, state, f"⚠️ {e}\nStep 4/4 — size per level:", wiz_size_kb())
        return
    await _go_confirm(bot, state, size)


# Step 5 — confirm / launch
@router.callback_query(GridWizard.confirm, WizCB.filter(F.field == "confirm"))
async def wiz_confirm(cb: CallbackQuery, api: WorkerAPI, state: FSMContext, bot: Bot) -> None:
    cfg = await _cfg(state)
    await cb.answer("Launching…")
    text, iid = await commands.create_grid_result(
        api, _uid(cb), cfg["market"], cfg["band"], cfg["levels"], cfg["size"])
    await _send_step(bot, state, text, launched_kb(iid) if iid else back_kb())
    await state.clear()


# 🧹 Cancel — abort the wizard from any step
@router.callback_query(WizCB.filter(F.field == "cancel"))
async def wiz_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer("Cancelled")
    if isinstance(cb.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.delete()


# ── run ──────────────────────────────────────────────────────────────────────

async def _set_commands(bot: Bot) -> None:
    await bot.set_my_commands(_COMMANDS)
    with contextlib.suppress(Exception):
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def _run(settings: BotSettings) -> None:
    api = WorkerAPI(settings.api_url)
    bot = Bot(settings.token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage(), api=api, settings=settings)
    allow = settings.allowed_ids()
    router.message.middleware(Allowlist(allow))
    router.callback_query.middleware(Allowlist(allow))
    dp.include_router(router)
    await _set_commands(bot)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
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
