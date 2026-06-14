"""Wizard parsers (pure) + keyboard construction — offline, no aiogram runtime."""
import pytest

from perpsbot import keyboards as kb
from perpsbot.wizard import parse_band_pct, parse_levels, parse_market, parse_size


def test_parse_market_normalises_and_validates():
    assert parse_market(" btcusdt ") == "BTCUSDT"
    for bad in ("", "   ", "BTC USDT", "X" * 21):
        with pytest.raises(ValueError):
            parse_market(bad)


def test_parse_band_pct_returns_fraction_string():
    assert parse_band_pct("1") == "0.01"
    assert parse_band_pct("0.8") == "0.008"
    assert parse_band_pct("±2%") == "0.02"
    for bad in ("0", "-1", "15", "abc"):
        with pytest.raises(ValueError):
            parse_band_pct(bad)


def test_parse_levels_bounds():
    assert parse_levels("10") == 10
    for bad in ("1", "201", "9.5", "x"):
        with pytest.raises(ValueError):
            parse_levels(bad)


def test_parse_size_keeps_text_form():
    assert parse_size("0.001") == "0.001"
    for bad in ("0", "-2", "abc"):
        with pytest.raises(ValueError):
            parse_size(bad)


def test_main_menu_kb_has_every_action():
    actions = {b.callback_data.split(":", 1)[1] for row in kb.main_menu_kb().inline_keyboard
               for b in row}
    assert {"refresh", "new_grid", "grids", "balance", "price", "help",
            "close"} <= actions


def test_grids_kb_pause_hidden_when_winding_down():
    rows = [{"instance_id": "BTCUSDT-0-aa", "state": "RUNNING"},
            {"instance_id": "ETHUSDT-0-bb", "state": "HALTED"}]
    labels = [b.text for row in kb.grids_kb(rows).inline_keyboard for b in row]
    assert any("Pause aa" in t for t in labels)        # running grid -> pausable
    assert not any("Pause bb" in t for t in labels)    # halted grid -> stop only
    assert any("Stop bb" in t for t in labels)


def test_callback_payloads_stay_under_telegram_limit():
    # 64-byte ceiling on callback_data; long-ish instance ids must still fit.
    iid = "BTCUSDT-0-abcdef123456"
    for markup in (kb.grids_kb([{"instance_id": iid, "state": "RUNNING"}]),
                   kb.stop_confirm_kb(iid), kb.launched_kb(iid)):
        for row in markup.inline_keyboard:
            for b in row:
                if b.callback_data:
                    assert len(b.callback_data.encode()) <= 64
