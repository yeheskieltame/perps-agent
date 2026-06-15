"""User-tunable knob registry (app/prefs.py): validation, precedence, and the
engine pieces it builds — plus settings persistence in the SQLite store."""
from decimal import Decimal

import pytest

from perpsagent.adapters.store.sqlite_store import SqliteStore
from perpsagent.app import prefs


# ---- validation ----

def test_defaults_are_self_valid():
    # every default must pass its own validator (registry can't ship a bad default)
    assert prefs.validate_updates({k: d for k, (d, _) in prefs.KNOBS.items()}) \
        == {k: d for k, (d, _) in prefs.KNOBS.items()}


def test_aliases_normalize():
    out = prefs.validate_updates({"lev": "25", "tf": "1h", "dd": "3", "arm": "0.2"})
    assert out == {"leverage": "25", "timeframe": "1h", "max_drawdown": "3", "trail_arm": "0.2"}


def test_round_numbers_render_plain_not_scientific():
    # '100'/'20'/'1000' must NOT come back as '1E+2'/'2E+1'/'1E+3' (ugly in the UI)
    out = prefs.validate_updates({"size": "100", "leverage": "20", "max_inventory": "1000"})
    assert out == {"size": "100", "leverage": "20", "max_inventory": "1000"}


def test_anchor_defaults_to_mean_and_validates():
    assert prefs.merged(None)["anchor"] == "mean"            # smart center by default
    assert prefs.anchor_mode({}) == "mean" and prefs.anchor_mode({"anchor": "now"}) == "now"
    assert prefs.validate_updates({"anchor": "now"}) == {"anchor": "now"}
    with pytest.raises(ValueError):
        prefs.validate_updates({"anchor": "yesterday"})


def test_unknown_key_and_bad_values_rejected():
    with pytest.raises(ValueError, match="unknown setting"):
        prefs.validate_updates({"banana": "1"})
    for key, bad in [("band", "0"), ("band", "11"), ("levels", "1"), ("levels", "999"),
                     ("size", "-1"), ("leverage", "0"), ("bias", "up"),
                     ("timeframe", "7m"), ("trail", "1.5"), ("tp", "x")]:
        with pytest.raises(ValueError):
            prefs.validate_updates({key: bad})


def test_merged_precedence_defaults_saved_overrides():
    s = prefs.merged({"leverage": "10", "band": "2"}, {"band": "0.5"})
    assert s["leverage"] == "10"      # saved beats default
    assert s["band"] == "0.5"         # override beats saved
    assert s["levels"] == "10"        # untouched default
    s2 = prefs.merged({"junk": "x"})  # unknown saved keys are dropped, not crashed
    assert "junk" not in s2


# ---- engine-unit conversions ----

def test_band_percent_to_fraction_and_grid_fields():
    s = prefs.merged({"band": "1.5", "levels": "4", "size": "120", "leverage": "25",
                      "bias": "long"})
    assert prefs.band_fraction(s) == Decimal("0.015")
    f = prefs.grid_fields(s)
    assert f == {"levels": 4, "order_size": Decimal("120"),
                 "leverage": Decimal("25"), "bias": 1}
    assert prefs.grid_fields(prefs.merged({"bias": "short"}))["bias"] == -1


def test_monitor_interval_follows_timeframe():
    assert prefs.monitor_interval(prefs.merged({"timeframe": "1m"})) == 15.0   # floor
    assert prefs.monitor_interval(prefs.merged({"timeframe": "1h"})) == 900.0  # bar/4
    assert prefs.monitor_interval(prefs.merged({"recenter": "30"})) == 30.0    # explicit
    assert prefs.monitor_interval(prefs.merged({"recenter": "0"})) == 0.0      # off


def test_build_guards_auto_inventory_and_profit():
    s = prefs.merged({"size": "0.01", "levels": "10", "tp": "0.5", "trail": "0.3",
                      "trail_arm": "0.2", "account_dd": "5"})
    breaker, profit, account = prefs.build_guards(s)
    assert breaker.max_inventory == Decimal("0.3")  # 0 = auto: size * levels * 3
    assert profit.take_profit == Decimal("0.5") and profit.trail_frac == Decimal("0.3")
    assert account is not None and account.max_drop == Decimal("5")
    b2, _, a2 = prefs.build_guards(prefs.merged({"max_inventory": "7"}))
    assert b2.max_inventory == Decimal("7")  # explicit cap wins over auto
    assert a2 is None                        # account_dd 0 -> no account guard


# ---- store persistence (SQLite; Postgres mirrors the same three methods) ----

@pytest.mark.asyncio
async def test_settings_roundtrip_survives_reopen(tmp_path):
    db = str(tmp_path / "s.db")
    store = SqliteStore(db)
    assert await store.get_settings(42) is None
    await store.put_settings(42, '{"leverage": "25"}')
    await store.put_settings(42, '{"leverage": "10"}')  # upsert

    store2 = SqliteStore(db)  # reopen → durable
    assert await store2.get_settings(42) == '{"leverage": "10"}'
    await store2.delete_settings(42)
    assert await store2.get_settings(42) is None
