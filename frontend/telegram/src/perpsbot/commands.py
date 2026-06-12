"""Command logic — pure: takes the API client + sender id + raw args, returns the
reply string (HTML). No aiogram imports, so every path is unit-testable offline.
"""
from __future__ import annotations

from .api import ApiError

HELP = (
    "<b>Perps Agent</b> — verifiable grid trading.\n"
    "Executes on Bybit; commits, learns and proves on Mantle.\n\n"
    "<b>Commands</b>\n"
    "/start — the dashboard: every button on one screen\n"
    "/connect — link your own Bybit API keys (DM only)\n"
    "/disconnect — forget your keys\n"
    "/grid MARKET [overrides] — launch a grid with YOUR settings\n"
    "    e.g. <code>/grid BTCUSDT</code> or <code>/grid MNTUSDT band=1.5 lev=25</code>\n"
    "/settings — your strategy settings (band, leverage, take-profit, ...)\n"
    "/set KEY VALUE — change one setting, e.g. <code>/set leverage 25</code>\n"
    "/set reset — back to defaults\n"
    "/status — your grids (state, pnl, fills)\n"
    "/stop INSTANCE — cancel orders, close the grid\n"
    "/pause INSTANCE — halt new orders\n"
    "/price MARKET — live top-of-book\n"
    "/balance — venue equity\n"
    "/health — backend status\n"
    "/cancel — abort the current dialog"
)

# /connect dialog texts (the aiogram FSM in main.py drives these states)
CONNECT_DM_ONLY = ("🔒 API keys are secrets — run /connect in a <b>direct message</b> "
                   "with me, never in a group.")
CONNECT_ASK_KEY = ("🔑 Send your <b>Bybit API key</b> now.\n"
                   "Create one at Bybit → API Management with <b>Contract Trade only</b> "
                   "(orders + positions). <b>Never enable Withdrawal.</b>\n"
                   "I delete your messages right after reading them. /cancel to abort.")
CONNECT_ASK_SECRET = "Now send the <b>API secret</b>."
CONNECT_ASK_ENV = ("Last step — which environment are these keys for?\n"
                   "Reply <code>testnet</code> (paper money, recommended first) or "
                   "<code>mainnet</code> (REAL money).")
CONNECT_BAD_ENV = "Reply <code>testnet</code> or <code>mainnet</code> (or /cancel)."
CONNECT_CANCELLED = "✖️ Cancelled — nothing was stored."

_STATE_ICON = {"RUNNING": "🟢", "REBALANCING": "🔄", "HALTED": "🔴",
               "EXITING": "🟠", "INITIALIZING": "⏳"}


def _err(e: ApiError) -> str:
    if e.status == 401:
        return "🔑 Not connected — link your Bybit API keys first with /connect."
    return f"⚠️ Backend refused ({e.status}): {e.detail or 'no detail'}"


def parse_env_choice(text: str) -> bool | None:
    """'testnet' -> True, 'mainnet' -> False, anything else -> None (re-ask).
    Explicit on purpose: mainnet must never be a default or a typo."""
    t = text.strip().lower()
    if t == "testnet":
        return True
    if t == "mainnet":
        return False
    return None


async def connect_start(api, user_id: int) -> str:
    """Opening message of the /connect dialog — shows the current link, if any."""
    try:
        info = await api.get_credentials(user_id)
    except ApiError as e:
        return _err(e)
    prefix = ""
    if info.get("connected"):
        env = "testnet" if info.get("testnet", True) else "mainnet"
        prefix = (f"ℹ️ Already connected ({env}, key <code>{info.get('key_preview', '?')}</code>) "
                  f"— continuing will overwrite it.\n\n")
    return prefix + CONNECT_ASK_KEY


async def connect_finish(api, user_id: int, api_key: str, api_secret: str,
                         testnet: bool) -> str:
    try:
        await api.put_credentials(user_id, api_key, api_secret, testnet=testnet)
    except ApiError as e:
        return _err(e)
    env = "testnet" if testnet else "⚠️ MAINNET (real money)"
    return (f"✅ <b>Connected</b> — {env}.\n"
            f"Your keys are encrypted at rest and never echoed back.\n"
            f"Try /balance to verify, then /grid MARKET to launch.")


async def disconnect(api, user_id: int) -> str:
    try:
        await api.delete_credentials(user_id)
    except ApiError as e:
        return _err(e)
    return "🗑 Keys forgotten and session closed. /connect to link again."


# Settings card: group -> [(key, hint)]. Keys mirror the backend knob registry
# (backend app/prefs.py); the card just renders whatever the backend returns.
SETTING_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Grid shape", [("band", "± percent around price"),
                    ("levels", "grid levels"),
                    ("size", "base qty per level")]),
    ("Risk", [("leverage", "1..100"),
              ("max_inventory", "net-position cap · 0 = auto"),
              ("max_drawdown", "loss cap, quote · 0 = off"),
              ("account_dd", "wallet kill-switch, quote · 0 = off")]),
    ("Exit", [("tp", "bank when PnL ≥ this, quote · 0 = off"),
              ("trail", "give-back fraction of peak · 0 = off"),
              ("trail_arm", "peak needed before trail arms")]),
    ("Behavior", [("bias", "long / short / neutral"),
                  ("timeframe", "1m 5m 15m 1h 4h 1d"),
                  ("recenter", "supervisor cadence, s · auto = bar/4")]),
]

GRID_USAGE = ("Usage: <code>/grid MARKET [KEY=VALUE ...]</code>\n"
              "e.g. <code>/grid BTCUSDT</code> (your /settings) or "
              "<code>/grid MNTUSDT band=1.5 levels=4 lev=25</code>\n"
              "Old style still works: <code>/grid BTCUSDT 1 10 0.001</code> "
              "= ±1% band, 10 levels, 0.001/level")


def settings_card(settings: dict, customized: list[str] | None = None) -> str:
    """Render the grouped settings card. Keys the user changed get a ✏️ marker."""
    custom = set(customized or [])
    lines = ["⚙️ <b>Your strategy settings</b> — every new /grid uses these"]
    shown = set()
    for group, keys in SETTING_GROUPS:
        rows = [(k, hint) for k, hint in keys if k in settings]
        if not rows:
            continue
        lines.append(f"\n<b>{group}</b>")
        for k, hint in rows:
            mark = " ✏️" if k in custom else ""
            lines.append(f"  <code>{k} = {settings[k]}</code>{mark} — {hint}")
            shown.add(k)
    for k in sorted(set(settings) - shown):  # backend added a knob the card doesn't know
        lines.append(f"  <code>{k} = {settings[k]}</code>")
    lines.append("\nChange one: <code>/set KEY VALUE</code> · reset: <code>/set reset</code>\n"
                 "Override once: <code>/grid MARKET KEY=VALUE ...</code>")
    return "\n".join(lines)


# ---- one-screen dashboard (ui.py renders; these gather/act, error-tolerant) ----

async def dashboard_data(api, user_id: int) -> tuple[dict | None, dict | None, list[dict]]:
    """(credentials, balance, grids) for the home screen. Each call degrades
    independently — a 401 means 'not connected', never a broken dashboard."""
    creds = balance = None
    grids: list[dict] = []
    try:
        creds = await api.get_credentials(user_id)
    except ApiError:
        pass
    if creds and creds.get("connected"):
        try:
            balance = await api.balance(user_id)
        except ApiError:
            balance = None
    try:
        grids = list(await api.status(user_id))
    except ApiError:
        pass
    return creds, balance, grids


async def launch_note(api, user_id: int, market: str) -> str:
    """Launch with the user's saved settings; one-line result for the dashboard."""
    try:
        resp = await api.create_grid(user_id, market.upper())
    except ApiError as e:
        return _err(e)
    eff = resp.get("effective", {})
    return (f"✅ Launched <code>{resp['instance_id']}</code> — "
            f"{eff.get('levels', '?')} levels · lev {eff.get('leverage', '?')}x · "
            f"[{resp.get('lower', '?')}, {resp.get('upper', '?')}]")


async def stop_note(api, user_id: int, instance_id: str) -> str:
    try:
        await api.stop(user_id, instance_id)
    except ApiError as e:
        return _err(e)
    return f"🛑 Stopped <code>{instance_id}</code> — orders cancelled."


async def price_toast(api, user_id: int, market: str) -> str:
    """Plain text (Telegram toasts don't render HTML)."""
    try:
        m = await api.market(user_id, market.upper())
    except ApiError as e:
        return f"{market}: {e.detail or e.status}"
    return f"{m['market']}  mid {m['mid']}  (bid {m['bid']} / ask {m['ask']})"


async def cycle_setting(api, user_id: int, key: str, next_value) -> tuple[str, dict]:
    """Cycle an enumerable knob to its next preset. Returns (plain toast, new
    settings dict) — the caller re-renders the settings screen."""
    try:
        current = (await api.get_settings(user_id)).get("settings", {})
        new = next_value(key, current.get(key, ""))
        resp = await api.put_settings(user_id, {key: new})
    except ApiError as e:
        return (e.detail or f"error {e.status}", {})
    return (f"{key} → {new}", resp.get("settings", {}))


async def settings_show(api, user_id: int) -> str:
    try:
        resp = await api.get_settings(user_id)
    except ApiError as e:
        return _err(e)
    return settings_card(resp.get("settings", {}), resp.get("customized"))


async def set_value(api, user_id: int, args: str) -> str:
    """/set — show card; /set KEY VALUE (or KEY=VALUE) — change one; /set reset."""
    parts = args.replace("=", " ").split()
    if not parts:
        return await settings_show(api, user_id)
    if parts[0].lower() == "reset":
        try:
            resp = await api.reset_settings(user_id)
        except ApiError as e:
            return _err(e)
        return "↩️ Settings reset to defaults.\n\n" + settings_card(resp.get("settings", {}))
    if len(parts) != 2:
        return ("Usage: <code>/set KEY VALUE</code>, e.g. <code>/set leverage 25</code>\n"
                "See your keys with /settings · <code>/set reset</code> for defaults")
    key, value = parts
    try:
        resp = await api.put_settings(user_id, {key: value})
    except ApiError as e:
        if e.status == 400:  # validation message is user-facing by design
            return f"❌ {e.detail or 'invalid setting'}"
        return _err(e)
    changed = ", ".join(f"<code>{k} = {resp['settings'][k]}</code>"
                        for k in resp.get("updated", []) if k in resp.get("settings", {}))
    return (f"✅ Saved: {changed}\n"
            f"Applies to every NEW grid (running grids keep their config). /settings to review.")


def _parse_grid_args(args: str) -> tuple[str, dict] | str:
    """'MARKET [BAND% [LEVELS [SIZE]]] [KEY=VALUE ...]' -> (market, overrides),
    or a user-facing error string. Validation of values is backend-side; only
    shape errors are caught here so nothing reaches the API malformed."""
    parts = args.split()
    if not parts:
        return GRID_USAGE
    market, rest = parts[0].upper(), parts[1:]
    overrides: dict[str, str] = {}
    positional = ("band", "levels", "size")
    pos = 0
    for tok in rest:
        if "=" in tok:
            key, _, value = tok.partition("=")
            if not key or not value:
                return f"Could not parse <code>{tok}</code>. " + GRID_USAGE
            overrides[key.lower()] = value
            pos = len(positional)  # key=value ends the positional section
        elif pos < len(positional):
            overrides[positional[pos]] = tok
            pos += 1
        else:
            return f"Unexpected argument <code>{tok}</code>. " + GRID_USAGE
    return market, overrides


async def grid(api, user_id: int, args: str) -> str:
    parsed = _parse_grid_args(args)
    if isinstance(parsed, str):
        return parsed
    market, overrides = parsed
    try:
        resp = await api.create_grid(user_id, market, overrides or None)
    except ApiError as e:
        if e.status == 400:
            return f"❌ {e.detail or 'invalid grid arguments'}\n{GRID_USAGE}"
        return _err(e)
    iid, eff = resp["instance_id"], resp.get("effective", {})
    lines = ["✅ <b>Grid launched</b>", f"instance: <code>{iid}</code>"]
    if eff:
        risk = f"lev {eff.get('leverage', '?')}x · bias {eff.get('bias', '?')} · tf {eff.get('timeframe', '?')}"
        exits = []
        if eff.get("tp", "0") != "0":
            exits.append(f"tp {eff['tp']}")
        if eff.get("trail", "0") != "0":
            exits.append(f"trail {eff['trail']}")
        lines.append(f"{market} [{resp.get('lower', '?')}, {resp.get('upper', '?')}] · "
                     f"{eff.get('levels', '?')} levels · {eff.get('size', '?')}/level")
        lines.append(risk + (" · " + " ".join(exits) if exits else " · exit guards off"))
    else:
        lines.append(market)
    lines.append(f"Use /status to follow it, /stop <code>{iid}</code> to close.")
    return "\n".join(lines)


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
