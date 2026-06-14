"""The /grid builder state machine + pure input parsers (from PR #48).

The parsers are pure (str -> value or ValueError) so they're unit-tested without
aiogram; the handlers in main.py catch ValueError and re-prompt. Validation mirrors
the typed `/grid` path; the backend re-validates everything anyway (400/409).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from aiogram.fsm.state import State, StatesGroup


class GridWizard(StatesGroup):
    market = State()
    strategy = State()   # pick a named preset, or go manual
    band = State()
    levels = State()
    size = State()
    confirm = State()

# Named strategies (safe/balanced/aggressive) live BACKEND-side now (app/prefs.py
# STRATEGY_TEMPLATES) because they auto-size from the user's balance; the wizard just
# passes the chosen template name. The manual path below still uses these parsers.


def parse_market(text: str) -> str:
    market = text.strip().upper()
    if not market or " " in market or len(market) > 20:
        raise ValueError("Market must be a short symbol like BTCUSDT.")
    return market


def parse_band_pct(text: str) -> str:
    """Accept a percent (1 = ±1%) and return the half-band FRACTION as a string
    ('0.01'), matching the legacy create-grid body / the wizard's internal cfg."""
    try:
        pct = Decimal(text.strip().lstrip("±").rstrip("%"))
    except InvalidOperation as exc:
        raise ValueError("Band must be a number, e.g. 1 for ±1%.") from exc
    if not Decimal(0) < pct <= Decimal(10):
        raise ValueError("Band must be in (0, 10] percent.")
    return str(pct / 100)


def parse_levels(text: str) -> int:
    try:
        levels = int(text.strip())
    except ValueError as exc:
        raise ValueError("Levels must be a whole number.") from exc
    if not 2 <= levels <= 200:
        raise ValueError("Levels must be 2..200.")
    return levels


def parse_size(text: str) -> str:
    try:
        qty = Decimal(text.strip())
    except InvalidOperation as exc:
        raise ValueError("Size must be a number.") from exc
    if qty <= 0:
        raise ValueError("Size must be positive.")
    return text.strip()
