"""Command logic — pure: takes the API client + sender id + raw args, returns the
reply string (HTML). No aiogram imports, so every path is unit-testable offline.
"""
from __future__ import annotations

from decimal import Decimal

from .api import ApiError

HELP = (
    "<b>Perps Agent</b> — a grid bot that buys low & sells high for you, over and over.\n"
    "Runs on Bybit; records every move on Mantle so the track record is provable.\n\n"
    "<b>New? Easiest path:</b> tap 🚀 <b>New Grid</b> → pick a coin → pick "
    "🛡 Safe / ⚖️ Balanced / 🔥 Aggressive → Launch. That's it.\n\n"
    "<b>Commands</b>\n"
    "/start — the dashboard: every button on one screen\n"
    "/connect — link your own Bybit API keys (DM only)\n"
    "/disconnect — forget your keys\n"
    "/grid MARKET [overrides] — launch a grid with YOUR settings\n"
    "    e.g. <code>/grid BTCUSDT</code> or <code>/grid MNTUSDT band=1.5 lev=25</code>\n"
    "    ✏️ Or tap New Grid → <b>Set it myself</b> to tune every value by tapping.\n"
    "/status — your grids (state, pnl, fills)\n"
    "/positions — live Bybit positions (size, entry, mark, PnL) + close\n"
    "/orders — your open Bybit orders\n"
    "/stop INSTANCE — cancel orders, close the grid\n"
    "/pause INSTANCE — halt new orders\n"
    "/price MARKET — live top-of-book\n"
    "/balance — venue equity\n"
    "/wallet — your MNT wallet (pays builder fees on Mantle)\n"
    "/topup — fund your MNT wallet (faucet link)\n"
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

# Mantle Sepolia explorer — the verifiable side of the product. The bot shows the
# proof tx so the on-chain commit/attest is visible right in the chat (the backend
# owns the chain; this is display only).
MANTLE_EXPLORER = "https://sepolia.mantlescan.xyz"


def _tx_link(tx: str, label: str) -> str:
    tx = tx if tx.startswith("0x") else "0x" + tx          # explorers want the 0x prefix
    short = f"{tx[:10]}…{tx[-6:]}" if len(tx) > 18 else tx
    return f'<a href="{MANTLE_EXPLORER}/tx/{tx}">{label} {short}</a>'


def _proof_line(proofs: dict | None, kind: str, prefix: str) -> str:
    """One on-chain proof line, or '' when proofs are absent (chain disabled)."""
    tx = (proofs or {}).get(kind)
    return f"\n{prefix} · {_tx_link(tx, 'tx')}" if tx else ""


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
                    ("size", "base qty per level"),
                    ("anchor", "center: smart (recent avg) or now")]),
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
              "e.g. <code>/grid BTCUSDT</code> (your saved knobs) or "
              "<code>/grid MNTUSDT band=1.5 levels=4 lev=25</code>\n"
              "Old style still works: <code>/grid BTCUSDT 1 10 0.001</code> "
              "= ±1% band, 10 levels, 0.001/level")


def grid_build_card(market: str, settings: dict) -> str:
    """The 'Set it myself' builder header + the grouped knobs for THIS grid. Tap a
    value below to change it, then 🚀 Launch."""
    lines = [f"➕ <b>New grid · {market}</b> — set it your way.",
             "<b>Tap any value below to change it</b>, then 🚀 Launch.",
             "<i>Not sure? ⬅️ Styles has one-tap presets.</i>"]
    shown = set()
    for group, keys in SETTING_GROUPS:
        rows = [(k, hint) for k, hint in keys if k in settings]
        if not rows:
            continue
        lines.append(f"\n<b>{group}</b>")
        for k, hint in rows:
            lines.append(f"  <code>{k} = {settings[k]}</code> — {hint}")
            shown.add(k)
    for k in sorted(set(settings) - shown):  # backend added a knob the card doesn't know
        lines.append(f"  <code>{k} = {settings[k]}</code>")
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


async def menu_snapshot(api, user_id: int) -> str:
    """The one-screen home info card (Polymarket-style): connection · Bybit equity ·
    MNT wallet · positions. Every backend call degrades independently."""
    creds, balance, grids = await dashboard_data(api, user_id)
    wallet = None
    try:
        wallet = await api.wallet(user_id)
    except ApiError:
        pass
    lines = ["🤖 <b>Perps Agent</b> — verifiable grid trading on Mantle"]
    if creds and creds.get("connected"):
        env = "testnet" if creds.get("testnet", True) else "⚠️ MAINNET"
        eq = f" · 💰 <b>{balance['equity']} {balance['currency']}</b>" if balance else ""
        lines.append(f"🔑 Bybit {env} ({creds.get('key_preview', '?')}){eq}")
    else:
        lines.append("🔑 <b>Not connected</b> — tap 🔑 Connect Bybit keys")
    if wallet:
        lines.append(f"👛 MNT wallet: <b>{_mnt(wallet.get('balance', 0))} MNT</b> — pays fees (💧 Top up)")
    if grids:
        lines.append(f"\n📊 <b>Positions</b> · {len(grids)} grid(s)")
        for g in grids[:6]:
            icon = _STATE_ICON.get(g["state"], "•")
            label = g.get("name") or g["instance_id"]
            lines.append(f"{icon} <b>{label}</b>\n"
                         f"     {g['state']} · pnl {g['realized_pnl']} · fills {g['fill_count']}")
    else:
        lines.append("\n📭 No grids running — tap 🚀 <b>New Grid</b> to launch one")
    return "\n".join(lines)


def render_history(rows: list[dict]) -> str:
    """Recently closed grids: pnl, fills, winrate per episode."""
    if not rows:
        return "📜 <b>History</b>\nNo closed grids yet — your stopped grids will show here."
    lines = ["📜 <b>History</b> — recently closed grids"]
    for r in rows:
        wr = round(float(r.get("winrate", 0)) * 100)
        label = r.get("name") or r["instance_id"]
        lines.append(f"• <b>{label}</b> · {r.get('market', '?')}\n"
                     f"     pnl <b>{r.get('realized_pnl', '0')}</b> · fills {r.get('fill_count', 0)} · win {wr}%")
    return "\n".join(lines)


def _signed(s) -> str:
    """Money with an explicit sign + thousands, 2dp: +12.34 / −1.20."""
    v = Decimal(str(s))
    return f"{'+' if v >= 0 else '−'}{abs(v):,.2f}"


def _base_of(market: str) -> str:
    """BTCUSDT -> BTC (the coin a position is denominated in)."""
    for q in ("USDT", "USDC", "USD"):
        if market.endswith(q):
            return market[: -len(q)]
    return market


def render_detail(d: dict) -> str:
    """Full single-grid view: headline PnL (USDT + %), position, setup, proofs."""
    icon = _STATE_ICON.get(d.get("state"), "•")
    name = d.get("name") or d["instance_id"]
    total = d.get("total_pnl", d.get("realized_pnl", "0"))
    tone = "🟢" if Decimal(str(total)) >= 0 else "🔴"
    lines = [
        f"📈 <b>{name}</b>  {icon} {d.get('state')}",
        f"<code>{d['instance_id']}</code>",
        "",
        f"{tone} <b>{_signed(total)} USDT</b>  ({_signed(d.get('pnl_pct', 0))}%)",
        f"     Realized {_signed(d.get('realized_pnl', 0))} · "
        f"Unrealized {_signed(d.get('unrealized_pnl', 0))} USDT",
    ]
    pos = Decimal(str(d.get("position", "0") or "0"))
    if pos != 0:
        lines.append(f"     Position {_num(abs(pos))} {_base_of(d.get('market', ''))} "
                     f"@ {_q2(d.get('avg_entry', 0))}")
    else:
        lines.append("     Position flat — waiting for fills")
    lines.append(f"     Fills {d.get('fill_count', 0)}")
    lines.append("\n⚙️ <b>Setup</b>")
    lower, upper = d.get("lower"), d.get("upper")
    if lower and upper:
        mid = (Decimal(str(lower)) + Decimal(str(upper))) / 2
        bpct = ((Decimal(str(upper)) - Decimal(str(lower))) / 2 / mid * 100) if mid else Decimal(0)
        lines.append(f"     Range ±{bpct:.2f}%  ({_q2(lower)} – {_q2(upper)})")
    lines.append(f"     {d.get('levels', '?')} steps · size {_num(d.get('order_size', '0'))} · "
                 f"lev x{_num(d.get('leverage', '1'))}")
    if d.get("margin"):
        lines.append(f"     Margin ~{_q2(d['margin'])} · notional ~{_q2(d.get('notional', 0))} USDT")
    proof_lines = [ln.lstrip("\n") for kind, lbl in (
        ("commit", "⛓ committed on-chain"), ("attest", "⛓ attested on-chain"),
        ("fee", "💸 builder fee on-chain")) if (ln := _proof_line(d.get("proofs", {}), kind, lbl))]
    if proof_lines:
        lines.append("")
        lines += proof_lines
    return "\n".join(lines)


async def detail(api, user_id: int, instance_id: str) -> tuple[str, dict | None]:
    """Fetch + render one grid's detail. Returns (text, detail|None)."""
    try:
        d = await api.grid_detail(user_id, instance_id)
    except ApiError as e:
        return (_err(e), None)
    return (render_detail(d), d)


async def history(api, user_id: int) -> str:
    try:
        return render_history(await api.history(user_id))
    except ApiError as e:
        return _err(e)


async def clear_stopped(api, user_id: int) -> str:
    try:
        n = (await api.clear_stopped(user_id)).get("cleared", 0)
    except ApiError as e:
        return _err(e)
    return f"🧹 Cleared {n} stopped grid(s)." if n else "Nothing to clear — no stopped grids."


def render_status(rows: list[dict]) -> str:
    """Grid list for the My-Grids view (rows already fetched by the caller)."""
    if not rows:
        return "📭 No grids running. Tap 🚀 New Grid to launch one."
    lines = ["📊 <b>Your grids</b> — tap one for details / edit"]
    for r in rows:
        icon = _STATE_ICON.get(r["state"], "•")
        label = r.get("name") or r["instance_id"]
        lines.append(f"{icon} <b>{label}</b> — {r['state']} · pnl {r['realized_pnl']} · fills {r['fill_count']}")
    return "\n".join(lines)


# ── plain-language wizard copy (a general user shouldn't need to know "band") ──

WIZ_MARKET = ("➕ <b>New grid</b> · Step 1 of 2\n"
              "Which coin do you want the bot to trade?\n"
              "Tap one below, or type a symbol like <code>BTCUSDT</code>.")

WIZ_STRATEGY = ("Step 2 of 2 — <b>How should it trade?</b>\n"
                "A grid bot quietly <b>buys a little when the price dips and sells when it "
                "rises</b>, over and over, pocketing the small difference.\n\n"
                "Pick a ready-made style — or set it yourself:\n"
                "🛡 <b>Safe</b> — narrow range, small steady gains, lower risk\n"
                "⚖️ <b>Balanced</b> — the all-rounder (recommended)\n"
                "🔥 <b>Aggressive</b> — wide range, bigger swings, more risk")

_STRATEGY_LABEL = {"safe": "🛡 Safe", "balanced": "⚖️ Balanced", "aggressive": "🔥 Aggressive"}

# Plain blurb per template (kept in sync with the backend STRATEGY_TEMPLATES). Size is
# auto-set from the user's balance, so we describe leverage + how much balance it uses.
STRATEGY_BLURB = {
    "safe": "Leverage <b>x1</b> · ~15% of your balance · tight <b>±0.5%</b> range · 10 steps",
    "balanced": "Leverage <b>x5</b> · ~35% of your balance · <b>±1%</b> range · 10 steps",
    "aggressive": "Leverage <b>x25</b> · ~80% of your balance · wide <b>±2%</b> range · 8 steps",
}


WIZ_MARGIN = ("💰 <b>How much to commit?</b> This is the MARGIN (your own balance) — "
              "leverage is applied on top.\nPick a % of your free balance, or ✏️ type an "
              "amount in USDT:")


def _q2(s) -> str:
    return f"{Decimal(str(s)):,.2f}"


def _num(s) -> str:
    return f"{Decimal(str(s)).normalize():f}"   # plain decimal — '20'/'250', not '2E+1'


def render_template_preview(plan: dict, template: str) -> str:
    """Review screen with the COMPUTED value (margin × leverage = notional, size/step)."""
    cur = plan.get("currency", "USDT")
    return (f"➕ <b>Review</b> · {_STRATEGY_LABEL.get(template, template)}\n"
            f"Coin: <b>{plan['market']}</b> · range {plan['lower']} – {plan['upper']}\n"
            f"Margin: <b>{_q2(plan['margin'])} {cur}</b> × lev x{plan['leverage']} "
            f"= notional <b>{_q2(plan['notional'])} {cur}</b>\n"
            f"{plan['levels']} steps · size <b>{_num(plan['size'])}</b>/step\n\n"
            f"Tap ✅ Launch — records the setup on-chain, then starts.")


async def create_template_result(api, user_id: int, market: str, template: str,
                                 margin: str | None = None,
                                 margin_pct: str | None = None) -> tuple[str, str | None]:
    """Launch a template grid; the worker sizes from margin (USDT) or margin_pct."""
    try:
        resp = await api.create_grid(user_id, market.upper(), template=template,
                                     margin=margin, margin_pct=margin_pct)
    except ApiError as e:
        if e.status == 401:
            return ("🔑 Connect your Bybit keys first with /connect.", None)
        return (f"❌ {e.detail or 'launch failed'}", None)
    iid, eff = resp["instance_id"], resp.get("effective", {})
    text = (f"✅ <b>Grid launched</b> · {_STRATEGY_LABEL.get(template, '')}\n<code>{iid}</code>\n"
            f"{market.upper()} [{resp.get('lower', '?')}, {resp.get('upper', '?')}] · "
            f"{eff.get('levels', '?')} levels · size {eff.get('size', '?')} · lev x{eff.get('leverage', '?')}"
            + _proof_line(resp.get("proofs"), "commit", "⛓ committed on-chain"))
    return text, iid


async def create_grid_result(api, user_id: int, market: str) -> tuple[str, str | None]:
    """Launch a grid from the user's saved knob settings — the 'Set it myself' builder
    edits those, snapshotted into the grid at launch. Returns (reply, instance_id|None)."""
    try:
        resp = await api.create_grid(user_id, market.upper())
    except ApiError as e:
        return (f"❌ {e.detail or 'invalid grid'}" if e.status == 400 else _err(e)), None
    iid, eff = resp["instance_id"], resp.get("effective", {})
    text = (f"✅ <b>Grid launched</b>\n<code>{iid}</code>\n"
            f"{market.upper()} [{resp.get('lower', '?')}, {resp.get('upper', '?')}] · "
            f"{eff.get('levels', '?')} levels · {eff.get('size', '?')}/level · "
            f"lev x{eff.get('leverage', '?')}"
            + _proof_line(resp.get("proofs"), "commit", "⛓ committed on-chain"))
    return text, iid


async def launch_note(api, user_id: int, market: str) -> str:
    """Launch with the user's saved settings; one-line result for the dashboard."""
    try:
        resp = await api.create_grid(user_id, market.upper())
    except ApiError as e:
        return _err(e)
    eff = resp.get("effective", {})
    return (f"✅ Launched <code>{resp['instance_id']}</code> — "
            f"{eff.get('levels', '?')} levels · lev {eff.get('leverage', '?')}x · "
            f"[{resp.get('lower', '?')}, {resp.get('upper', '?')}]"
            + _proof_line(resp.get("proofs"), "commit", "⛓ committed on-chain"))


async def stop_note(api, user_id: int, instance_id: str) -> str:
    try:
        resp = await api.stop(user_id, instance_id)
    except ApiError as e:
        return _err(e)
    proofs = resp.get("proofs") if isinstance(resp, dict) else None
    return (f"🛑 Stopped <code>{instance_id}</code> — orders cancelled."
            + _proof_line(proofs, "attest", "⛓ outcome attested on-chain")
            + _proof_line(proofs, "fee", "💸 builder fee settled on-chain"))


async def price_toast(api, user_id: int, market: str) -> str:
    """Plain text (Telegram toasts don't render HTML)."""
    try:
        m = await api.market(user_id, market.upper())
    except ApiError as e:
        return f"{market}: {e.detail or e.status}"
    return f"{m['market']}  mid {m['mid']}  (bid {m['bid']} / ask {m['ask']})"


async def set_setting(api, user_id: int, key: str, val: str) -> tuple[str, dict | None]:
    """Apply one knob value (from a button tap). Returns (toast, new_settings|None).
    The backend validates; an invalid value comes back as a friendly toast."""
    try:
        resp = await api.put_settings(user_id, {key: val})
    except ApiError as e:
        return (f"❌ {e.detail or e.status}", None)
    return (f"{key} → {val} ✓", resp.get("settings", {}))


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
    commit = _proof_line(resp.get("proofs"), "commit", "⛓ committed on-chain")
    if commit:
        lines.append(commit.lstrip("\n"))
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
        resp = await api.stop(user_id, iid)
    except ApiError as e:
        return _err(e)
    proofs = resp.get("proofs") if isinstance(resp, dict) else None
    return (f"🛑 Stopped <code>{iid}</code> — orders cancelled."
            + _proof_line(proofs, "attest", "⛓ outcome attested on-chain")
            + _proof_line(proofs, "fee", "💸 builder fee settled on-chain"))


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


def render_positions(rows: list[dict]) -> str:
    """Live Bybit positions: side · size (~notional) · entry→mark · PnL (% vs entry)."""
    if not rows:
        return "📋 <b>Positions · Bybit</b>\nNo open positions right now."
    lines = ["📋 <b>Positions · Bybit</b>"]
    for r in rows:
        sicon = "🟢" if r["side"] == "LONG" else "🔴"
        pnl = Decimal(str(r["pnl"]))
        picon = "🟢" if pnl >= 0 else "🔴"
        pnl_disp = f"+${_q2(pnl)}" if pnl >= 0 else f"-${_q2(-pnl)}"
        pct = r["pnl_pct"]
        pct_disp = f"+{pct}" if pnl >= 0 else str(pct)
        lines.append(
            f"\n{sicon} <b>{r['market']}</b> · {r['side']}\n"
            f"Size {r['size']} (~${_q2(r['notional'])})\n"
            f"Entry {r['entry']} → Mark {r['mark']}\n"
            f"PnL {picon} {pnl_disp} ({pct_disp}% vs entry)")
    return "\n".join(lines)


def render_orders(rows: list[dict]) -> str:
    """Open venue orders grouped by market (🟢 buys / 🔴 sells), price high → low."""
    if not rows:
        return "📑 <b>Open orders · Bybit</b>\nNo open orders right now."
    lines = ["📑 <b>Open orders · Bybit</b>"]
    cur = None
    for r in rows:
        if r["market"] != cur:
            cur = r["market"]
            lines.append(f"\n<b>{cur}</b>")
        icon = "🟢" if str(r["side"]).lower().startswith("b") else "🔴"
        lines.append(f"{icon} {str(r['side']).upper()} {r['qty']} @ {r['price']}")
    return "\n".join(lines)


async def orders(api, user_id: int) -> str:
    try:
        return render_orders(await api.open_orders(user_id))
    except ApiError as e:
        return _err(e)


async def positions(api, user_id: int) -> tuple[str, list[dict] | None]:
    """Fetch + render Bybit positions. Returns (text, rows|None) — None on error."""
    try:
        rows = await api.positions(user_id)
    except ApiError as e:
        return (_err(e), None)
    return (render_positions(rows), rows)


async def close_position(api, user_id: int, market: str) -> str:
    try:
        await api.close_position(user_id, market)
    except ApiError as e:
        return _err(e)
    return f"❌ Closed your {market} position."


def position_card(p: dict, *, live: bool) -> dict:
    """PnL-card data for a position — live (unrealized, to share) or a realized close."""
    base = _base_of(p.get("market", ""))
    px = (f"Entry {_num(p.get('entry', '0'))}   ·   Mark {_num(p.get('mark', '0'))}" if live
          else f"Entry {_num(p.get('entry', '0'))}    →    Exit {_num(p.get('mark', '0'))}")
    return {"title": p.get("market", ""), "subtitle": p.get("side", ""),
            "badge": "LIVE" if live else None, "currency": "USDT",
            "pnl": p.get("pnl", "0"), "pnl_pct": p.get("pnl_pct", "0"),
            "lines": [f"Qty   {_num(p.get('size', '0'))} {base}", px]}


def grid_card(d: dict, *, live: bool) -> dict:
    """PnL-card data for a grid — live (running, total PnL) or a realized stop."""
    return {"title": d.get("name") or d.get("instance_id", ""), "subtitle": "GRID",
            "badge": "LIVE" if live else None, "currency": "USDT",
            "pnl": d.get("total_pnl", d.get("realized_pnl", "0")), "pnl_pct": d.get("pnl_pct", "0"),
            "lines": [f"{d.get('market', '')}   ·   {d.get('fill_count', 0)} fills",
                      f"Realized {_signed(d.get('realized_pnl', 0))} · "
                      f"Unrealized {_signed(d.get('unrealized_pnl', 0))} USDT"]}


async def close_with_card(api, user_id: int, market: str) -> tuple[str, dict | None]:
    """Snapshot the position's live PnL, then close it — so the caller can render a
    shareable PnL card. Returns (note, card_data | None if there was no position)."""
    card = None
    try:
        for p in await api.positions(user_id):
            if p.get("market") == market:
                card = position_card(p, live=False)
                break
    except ApiError:
        pass
    return await close_position(api, user_id, market), card


def pnl_caption(card: dict) -> str:
    """One-line caption under the PnL card (works for positions and grids)."""
    pnl = Decimal(str(card.get("pnl", "0")))
    tone = "🟢" if pnl >= 0 else "🔴"
    badge = card.get("badge") or ("WIN" if pnl >= 0 else "LOSS")
    return (f"{tone} <b>{card.get('title')} {card.get('subtitle')}</b> · {badge}\n"
            f"PnL <b>{_signed(card.get('pnl', 0))} {card.get('currency', 'USDT')}</b> "
            f"({_signed(card.get('pnl_pct', 0))}%)")


async def close_all_positions(api, user_id: int) -> str:
    try:
        n = (await api.close_all_positions(user_id)).get("closed", 0)
    except ApiError as e:
        return _err(e)
    return f"❌ Closed {n} position(s)." if n else "No open positions to close."


async def cancel_all_orders(api, user_id: int) -> str:
    try:
        n = (await api.cancel_all_orders(user_id)).get("markets", 0)
    except ApiError as e:
        return _err(e)
    return f"🧹 Cancelled all orders + stopped grids across {n} market(s)."


async def panic(api, user_id: int) -> str:
    try:
        r = await api.panic(user_id)
    except ApiError as e:
        return _err(e)
    return (f"🛑 <b>Flat &amp; out.</b>\nStopped {r.get('grids_stopped', 0)} grid(s), "
            f"closed {r.get('positions_closed', 0)} position(s), cancelled all orders.")


async def balance(api, user_id: int) -> str:
    try:
        b = await api.balance(user_id)
    except ApiError as e:
        return _err(e)
    return f"💰 Equity <b>{b['equity']} {b['currency']}</b> · available {b['available']}"


def _mnt(wei: str | int) -> str:
    return f"{int(wei) / 1e18:.4f}"


async def wallet(api, user_id: int) -> str:
    """Show the user's managed MNT wallet — the address that pays builder fees."""
    try:
        w = await api.wallet(user_id)
    except ApiError as e:
        if e.status == 503:
            return "👛 Wallet unavailable — the backend has no credential master key set."
        return _err(e)
    return (f"👛 <b>Your MNT wallet</b> — pays builder fees on Mantle\n"
            f"<code>{w['address']}</code>\n"
            f"Balance: <b>{_mnt(w.get('balance', 0))} MNT</b>\n\n"
            f"Low on MNT? <code>/topup</code> to fund it.")


async def topup(api, user_id: int) -> str:
    """How to fund the managed wallet: faucet link + the deposit address."""
    try:
        w = await api.wallet(user_id)
    except ApiError as e:
        if e.status == 503:
            return "👛 Wallet unavailable — the backend has no credential master key set."
        return _err(e)
    return (f"💧 <b>Top up your MNT wallet</b> (testnet)\n"
            f"1. Open the faucet: {w.get('faucet')}\n"
            f"2. Paste your address:\n<code>{w['address']}</code>\n"
            f"3. Claim testnet MNT — it pays your builder fees on Mantle.\n\n"
            f"Balance now: <b>{_mnt(w.get('balance', 0))} MNT</b> · re-check with /wallet")


async def health(api) -> str:
    try:
        h = await api.health()
    except ApiError as e:
        return _err(e)
    except Exception as e:  # backend down — connection refused etc.
        return f"🔌 Backend unreachable: {e}"
    return f"✅ Backend ok (node {h.get('node', '?')})"
