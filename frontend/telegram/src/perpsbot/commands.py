"""Command logic — pure: takes the API client + sender id + raw args, returns the
reply string (HTML). No aiogram imports, so every path is unit-testable offline.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .api import ApiError

HELP = (
    "<b>Perps Agent</b> — verifiable grid trading.\n"
    "Executes on Bybit; commits, learns and proves on Mantle.\n\n"
    "<b>Commands</b>\n"
    "/grid MARKET [BAND% [LEVELS [SIZE]]] — launch a grid\n"
    "    e.g. <code>/grid BTCUSDT</code> or <code>/grid BTCUSDT 0.8 12 0.002</code>\n"
    "/status — your grids (state, pnl, fills)\n"
    "/stop INSTANCE — cancel orders, close the grid\n"
    "/pause INSTANCE — halt new orders\n"
    "/price MARKET — live top-of-book\n"
    "/balance — venue equity\n"
    "/health — backend status"
)

_STATE_ICON = {"RUNNING": "🟢", "REBALANCING": "🔄", "HALTED": "🔴",
               "EXITING": "🟠", "INITIALIZING": "⏳"}


def _err(e: ApiError) -> str:
    return f"⚠️ Backend refused ({e.status}): {e.detail or 'no detail'}"


async def grid(api, user_id: int, args: str, *, band: str = "0.01",
               levels: int = 10, size: str = "0.001") -> str:
    parts = args.split()
    if not parts:
        return ("Usage: <code>/grid MARKET [BAND% [LEVELS [SIZE]]]</code>\n"
                "e.g. <code>/grid BTCUSDT 1 10 0.001</code> = ±1% band, 10 levels, 0.001/level")
    market = parts[0].upper()
    try:
        if len(parts) > 1:  # band given in PERCENT (1 = ±1%)
            pct = Decimal(parts[1])
            if not Decimal(0) < pct <= Decimal(10):
                return "Band must be in (0, 10] percent."
            band = str(pct / 100)
        if len(parts) > 2:
            levels = int(parts[2])
            if not 2 <= levels <= 200:
                return "Levels must be 2..200."
        if len(parts) > 3:
            if Decimal(parts[3]) <= 0:
                return "Size must be positive."
            size = parts[3]
    except (InvalidOperation, ValueError):
        return "Could not parse arguments. Usage: <code>/grid MARKET [BAND% [LEVELS [SIZE]]]</code>"
    try:
        iid = await api.create_grid(user_id, market, band, levels, size)
    except ApiError as e:
        return _err(e)
    pct_label = Decimal(band) * 100
    return (f"✅ <b>Grid launched</b>\n"
            f"instance: <code>{iid}</code>\n"
            f"{market} · ±{pct_label.normalize()}% band · {levels} levels · {size}/level\n"
            f"Use /status to follow it, /stop <code>{iid}</code> to close.")


async def status(api, user_id: int) -> str:
    try:
        rows = await api.status(user_id)
    except ApiError as e:
        return _err(e)
    if not rows:
        return "No grids yet. Launch one with /grid MARKET"
    lines = ["📊 <b>Your grids</b>"]
    for r in rows:
        icon = _STATE_ICON.get(r["state"], "•")
        lines.append(f"{icon} <code>{r['instance_id']}</code> — {r['state']}\n"
                     f"     pnl {r['realized_pnl']} · fills {r['fill_count']}")
    return "\n".join(lines)


async def stop(api, user_id: int, args: str) -> str:
    iid = args.strip()
    if not iid:
        return "Usage: <code>/stop INSTANCE</code> (find it via /status)"
    try:
        await api.stop(user_id, iid)
    except ApiError as e:
        return _err(e)
    return f"🛑 Stopped <code>{iid}</code> — orders cancelled."


async def pause(api, user_id: int, args: str) -> str:
    iid = args.strip()
    if not iid:
        return "Usage: <code>/pause INSTANCE</code>"
    try:
        await api.pause(user_id, iid)
    except ApiError as e:
        return _err(e)
    return f"⏸ Paused <code>{iid}</code>."


async def price(api, user_id: int, args: str) -> str:
    market = args.split()[0].upper() if args.split() else ""
    if not market:
        return "Usage: <code>/price MARKET</code>"
    try:
        m = await api.market(user_id, market)
    except ApiError as e:
        return _err(e)
    return f"💱 <b>{m['market']}</b> mid {m['mid']} (bid {m['bid']} / ask {m['ask']})"


async def balance(api, user_id: int) -> str:
    try:
        b = await api.balance(user_id)
    except ApiError as e:
        return _err(e)
    return f"💰 Equity <b>{b['equity']} {b['currency']}</b> · available {b['available']}"


async def health(api) -> str:
    try:
        h = await api.health()
    except ApiError as e:
        return _err(e)
    except Exception as e:  # backend down — connection refused etc.
        return f"🔌 Backend unreachable: {e}"
    return f"✅ Backend ok (node {h.get('node', '?')})"
