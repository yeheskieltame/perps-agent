"""One-screen dashboard: pure keyboard/text builders + the command helpers that
feed them. Offline — no aiogram."""
from perpsbot import commands, ui
from perpsbot.api import ApiError

from test_commands import DEFAULTS, FakeAPI


def _flat(rows: ui.Rows) -> list[tuple[str, str]]:
    return [btn for row in rows for btn in row]


def test_menu_shows_everything_on_one_screen():
    labels = [t for t, _ in _flat(ui.menu_rows(connected=True))]
    joined = " ".join(labels)
    for need in ("Launch", "Stop", "Settings", "Price", "Refresh", "Help", "Disconnect"):
        assert need in joined
    # not connected: Connect replaces Disconnect
    labels = " ".join(t for t, _ in _flat(ui.menu_rows(connected=False)))
    assert "Connect" in labels and "Disconnect" not in labels


def test_callback_data_is_namespaced_and_under_64_bytes():
    rows = (ui.menu_rows(True) + ui.market_rows(ui.DEFAULT_MARKETS, "g")
            + ui.settings_rows(DEFAULTS) + ui.stop_rows(["MNTUSDT-0-abcd1234"]))
    for _, data in _flat(rows):
        assert len(data.encode()) <= 64
        assert data.split(":", 1)[0] in {"d", "g", "p", "x", "s", "t"}


def test_every_knob_is_on_the_settings_screen():
    buttons = " ".join(t for t, _ in _flat(ui.settings_rows(DEFAULTS)))
    for key in DEFAULTS:
        assert key in buttons
    assert "Reset" in buttons and "Back" in buttons


def test_cycle_wraps_and_recovers_from_unknown():
    assert ui.next_value("bias", "neutral") == "long"
    assert ui.next_value("bias", "short") == "neutral"        # wraps
    assert ui.next_value("leverage", "7") == "1"              # unknown -> restart
    for key in ui.CYCLE:                                      # all presets validate-able
        assert ui.next_value(key, ui.CYCLE[key][-1]) == ui.CYCLE[key][0]


def test_dashboard_text_states():
    txt = ui.dashboard_text(None, None, [])
    assert "Not connected" in txt and "No grids" in txt
    txt = ui.dashboard_text({"connected": True, "testnet": False, "key_preview": "cR8x…"},
                            {"equity": "63.0", "currency": "USDT"},
                            [{"instance_id": "i1", "state": "RUNNING",
                              "realized_pnl": "0.2", "fill_count": 12}],
                            note="✅ Launched")
    assert "MAINNET" in txt and "63.0 USDT" in txt
    assert "🟢" in txt and "i1" in txt and txt.startswith("✅ Launched")


async def test_dashboard_data_degrades_per_call():
    api = FakeAPI()
    api.creds = {"connected": True, "testnet": True, "key_preview": "cR8x…"}
    creds, balance, grids = await commands.dashboard_data(api, 42)
    assert creds["connected"] and balance["equity"] == "73193.62" and grids == []

    api.fail = ApiError(401, "no venue credentials")   # everything fails -> still renders
    creds, balance, grids = await commands.dashboard_data(api, 42)
    assert creds is None and balance is None and grids == []
    assert "Not connected" in ui.dashboard_text(creds, balance, grids)


async def test_launch_stop_price_cycle_helpers():
    api = FakeAPI()
    note = await commands.launch_note(api, 42, "mntusdt")
    assert "Launched" in note and "MNTUSDT-0-abc123" in note
    assert ("create", 42, "MNTUSDT", None, None) in api.calls

    assert "Stopped" in await commands.stop_note(api, 42, "MNTUSDT-0-abc123")
    assert "mid 100.0" in await commands.price_toast(api, 42, "btcusdt")
    assert "<" not in await commands.price_toast(api, 42, "BTCUSDT")  # toasts: no HTML

    toast, new = await commands.cycle_setting(api, 42, "bias", ui.next_value)
    assert toast == "bias → long" and new["bias"] == "long"
    assert ("put_settings", 42, {"bias": "long"}) in api.calls


async def test_helpers_surface_errors_as_notes_not_raises():
    api = FakeAPI()
    api.fail = ApiError(401, "no venue credentials")
    assert "/connect" in await commands.launch_note(api, 42, "MNTUSDT")
    assert "/connect" in await commands.stop_note(api, 42, "i1")
    toast, new = await commands.cycle_setting(api, 42, "bias", ui.next_value)
    assert new == {} and toast  # toast carries the reason, screen stays put
