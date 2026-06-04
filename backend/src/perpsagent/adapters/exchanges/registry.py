"""Venue -> ExchangePort factory. THE place to add a new venue.

Adding a venue = implement ExchangePort in a sibling folder, then add one branch
here. The grid engine and the agent never change. (deltaperps adapter-registry.)
"""
from __future__ import annotations

from typing import Any

from ...domain.models import Venue
from ...domain.ports import ExchangePort


def make_exchange(venue: Venue, config: dict[str, Any]) -> ExchangePort:
    if venue is Venue.BYBIT:
        from .bybit.adapter import BybitExchange

        return BybitExchange(config)
    if venue is Venue.MANTLE_DEX:
        from .mantle_dex.adapter import MantleDexExchange

        return MantleDexExchange(config)
    if venue is Venue.FAKE:
        from .fake import FakeExchange

        return FakeExchange(config)
    raise ValueError(f"unknown venue: {venue}")
