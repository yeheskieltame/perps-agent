"""Render a shareable PnL card (PNG) over the brand template when a position closes.

Pure: a dict of trade data → PNG bytes. Uses the bundled Liberation fonts (SIL OFL)
so the card renders identically on the VPS without any system-font dependency.
"""
from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_DIR = Path(__file__).resolve().parents[2]          # frontend/telegram
_TEMPLATE = _DIR / "template.png"
_FONTS = _DIR / "assets" / "fonts"
_BOLD = _FONTS / "LiberationSans-Bold.ttf"
_REG = _FONTS / "LiberationSans-Regular.ttf"

_GREEN = (22, 199, 132)
_RED = (246, 70, 93)
_WHITE = (236, 241, 247)
_GREY = (152, 164, 180)
_X = 86                                              # left text column (clear of the graphic)


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def _dec(x) -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:  # noqa: BLE001
        return Decimal(0)


def _signed(v: Decimal, dp: int = 2) -> str:
    return f"{'+' if v >= 0 else '-'}{abs(v):,.{dp}f}"


def _num(x) -> str:
    return f"{_dec(x).normalize():f}"


def render(data: dict) -> bytes:
    """data: {symbol, side, qty, base, entry, exit, pnl, pnl_pct, currency}. → PNG bytes."""
    pnl, pct = _dec(data.get("pnl")), _dec(data.get("pnl_pct"))
    color = _GREEN if pnl >= 0 else _RED
    badge = "WIN" if pnl >= 0 else "LOSS"
    symbol = str(data.get("symbol", "")).upper()
    side = str(data.get("side", "")).upper()
    currency = data.get("currency", "USDT")

    im = Image.open(_TEMPLATE).convert("RGB")
    d = ImageDraw.Draw(im)
    d.text((_X, 196), f"{symbol}   ·   {side}   ·   {badge}", font=_font(_BOLD, 50), fill=_WHITE)
    d.text((_X, 284), f"{_signed(pct)}%", font=_font(_BOLD, 170), fill=color)
    d.text((_X, 520), f"{_signed(pnl)} {currency}", font=_font(_BOLD, 80), fill=color)
    d.text((_X, 652), f"Qty   {_num(data.get('qty'))} {data.get('base', '')}",
           font=_font(_REG, 42), fill=_GREY)
    d.text((_X, 716), f"Entry {_num(data.get('entry'))}    →    Exit {_num(data.get('exit'))}",
           font=_font(_REG, 42), fill=_GREY)

    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()
