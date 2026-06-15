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
    """data: {title, subtitle, badge?, pnl, pnl_pct, currency, lines: [str, ...]}. → PNG.
    badge defaults to WIN/LOSS from the pnl sign; pass 'LIVE' for an unrealized card."""
    pnl, pct = _dec(data.get("pnl")), _dec(data.get("pnl_pct"))
    color = _GREEN if pnl >= 0 else _RED
    badge = data.get("badge") or ("WIN" if pnl >= 0 else "LOSS")
    parts = [p for p in (str(data.get("title", ""))[:16].upper(),
                         str(data.get("subtitle", "")).upper(), badge) if p]

    im = Image.open(_TEMPLATE).convert("RGB")
    d = ImageDraw.Draw(im)
    d.text((_X, 196), "   ·   ".join(parts), font=_font(_BOLD, 48), fill=_WHITE)
    d.text((_X, 284), f"{_signed(pct)}%", font=_font(_BOLD, 170), fill=color)
    d.text((_X, 520), f"{_signed(pnl)} {data.get('currency', 'USDT')}", font=_font(_BOLD, 80), fill=color)
    y = 652
    for line in (data.get("lines") or [])[:2]:
        d.text((_X, y), str(line), font=_font(_REG, 42), fill=_GREY)
        y += 64

    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()
