"""The New-Grid wizard state machine + the market-symbol parser.

`parse_market` is pure (str -> value or ValueError) so it's unit-tested without
aiogram; the market handler in main.py catches ValueError and re-prompts. Grid
shape/risk knobs are tuned on the single-screen builder (GridWizard.build), whose
values the backend validates on launch.
"""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class GridWizard(StatesGroup):
    market = State()
    strategy = State()   # pick a named preset, or "Set it myself"
    margin = State()     # how much balance to commit (template path)
    confirm = State()    # review a template launch
    build = State()      # the Set-it-myself single-screen knob builder


def parse_market(text: str) -> str:
    market = text.strip().upper()
    if not market or " " in market or len(market) > 20:
        raise ValueError("Market must be a short symbol like BTCUSDT.")
    return market
