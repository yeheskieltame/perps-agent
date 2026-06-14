"""Command logic — pure: takes the API client + sender id + raw args, returns the
reply string (HTML). No aiogram imports, so every path is unit-testable offline.
The same renderers back BOTH the typed commands and the inline-keyboard UI.
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
    "/balance — venue equity\n\n"
    "Tip: <b>/menu</b> opens the button UI — no typing needed."
)

_STATE_ICON = {"RUNNING": "🟢", "REBALANCING": "🔄", "HALTED": "🔴",
               "EXITING": "🟠", "INITIALIZING": "⏳"}

# States that no longer place new orders — used for "active" counts and to hide
# the ⏸ Pause button on a grid that is already winding down.
_INACTIVE = {"HALTED", "EXITING"}


def _err(e: ApiError) -> str:
    return f"⚠️ Backend refused ({e.status}): {e.detail or 'no detail'}"


def _band_pct(band: str) -> Decimal:
    """Half-band fraction (0.008) -> percent for display (0.8)."""
    return (Decimal(band) * 100).normalize()


# ── grid creation ────────────────────────────────────────────────────────────

async def create_grid_result(api, user_id: int, market: str, band: str, levels: int,
                             size: str, leverage: str = "1") -> tuple[str, str | None]:
    """Place a grid. Returns (reply_text, instance_id|None) so callers that need the
    id — the wizard's launched-card buttons — get it without re-parsing the text."""
    try:
        iid = await api.create_grid(user_id, market, band, levels, size, leverage)
    except ApiError as e:
        return _err(e), None
    text = (f"✅ <b>Grid launched</b>\n"
            f"instance: <code>{iid}</code>\n"
            f"{market} · ±{_band_pct(band)}% band · {levels} levels · {size}/level\n"
            f"Use /status to follow it, /stop <code>{iid}</code> to close.")
    return text, iid


async def grid_launch(api, user_id: int, market: str, band: str, levels: int,
                      size: str, leverage: str = "1") -> str:
    text, _ = await create_grid_result(api, user_id, market, band, levels, size, leverage)
    return text


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
    return await grid_launch(api, user_id, market, band, levels, size)


# ── status ───────────────────────────────────────────────────────────────────

def render_status(rows: list[dict]) -> str:
    if not rows:
        return "No grids yet. Launch one with /grid MARKET"
    lines = ["📊 <b>Your grids</b>"]
    for r in rows:
        icon = _STATE_ICON.get(r["state"], "•")
        lines.append(f"{icon} <code>{r['instance_id']}</code> — {r['state']}\n"
                     f"     pnl {r['realized_pnl']} · fills {r['fill_count']}")
    return "\n".join(lines)


async def status(api, user_id: int) -> str:
    try:
        rows = await api.status(user_id)
    except ApiError as e:
        return _err(e)
    return render_status(rows)


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


# ── inline-UI render helpers (pure) ──────────────────────────────────────────

def grid_confirm_text(market: str, band: str, levels: int, size: str,
                      leverage: str = "1") -> str:
    return ("🧩 <b>Review grid</b>\n"
            f"{market} · ±{_band_pct(band)}% band · {levels} levels · {size}/level · "
            f"{leverage}× lev\n"
            "Tap <b>Launch</b> to place it.")


async def menu_snapshot(api, user_id: int) -> str:
    """Welcome + live snapshot for the main menu. Degrades gracefully when the worker
    is down (every read is best-effort) so the menu always renders."""
    lines = ["🤖 <b>Perps Agent</b> — verifiable grid trading",
             "Executes on Bybit; commits, learns &amp; proves on Mantle.", ""]
    try:
        rows = await api.status(user_id)
        active = sum(1 for r in rows if r.get("state") not in _INACTIVE)
        lines.append(f"📊 Active grids: <b>{active}</b>"
                     + (f" / {len(rows)} total" if rows else ""))
    except Exception:
        lines.append("📊 Active grids: <i>unavailable</i>")
    try:
        b = await api.balance(user_id)
        lines.append(f"💰 Equity: <b>{b['equity']} {b['currency']}</b>")
    except Exception:
        pass
    lines += ["", "Pick an action 👇"]
    return "\n".join(lines)
