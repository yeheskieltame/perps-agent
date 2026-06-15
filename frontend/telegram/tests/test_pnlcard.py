"""PnL share-card: the pure PNG renderer, the caption, and the close→snapshot flow."""
from perpsbot import commands, pnlcard
from tests.test_commands import FakeAPI

_PNG = b"\x89PNG\r\n\x1a\n"


def test_render_returns_a_png_for_win_and_loss():
    win = pnlcard.render({"symbol": "MNTUSDT", "side": "LONG", "qty": "250", "base": "MNT",
                          "entry": "0.5691", "exit": "0.5705", "pnl": "0.86", "pnl_pct": "2.34"})
    assert win[:8] == _PNG and len(win) > 1000          # a real PNG, not empty
    loss = pnlcard.render({"symbol": "BTCUSDT", "side": "SHORT", "qty": "0.01", "base": "BTC",
                           "entry": "65000", "exit": "65500", "pnl": "-5", "pnl_pct": "-0.77"})
    assert loss[:8] == _PNG


def test_pnl_caption_shows_signed_pnl_percent_and_token():
    cap = commands.pnl_caption({"symbol": "MNTUSDT", "side": "LONG", "pnl": "0.86",
                                "pnl_pct": "2.34", "qty": "250", "base": "MNT", "currency": "USDT"})
    assert "+0.86 USDT" in cap and "(+2.34%)" in cap and "250 MNT" in cap and "🟢" in cap


async def test_close_with_card_snapshots_pnl_then_closes():
    api = FakeAPI()
    api.pos = [{"market": "MNTUSDT", "side": "LONG", "size": "250", "entry": "0.5691",
                "mark": "0.5705", "pnl": "0.86", "pnl_pct": "2.34", "notional": "142"}]
    note, card = await commands.close_with_card(api, 42, "MNTUSDT")
    assert ("close_pos", 42, "MNTUSDT") in api.calls    # the position WAS closed
    assert card is not None
    assert card["pnl"] == "0.86" and card["exit"] == "0.5705" and card["base"] == "MNT"
    assert pnlcard.render(card)[:8] == _PNG             # the snapshot renders


async def test_close_with_card_no_position_returns_no_card():
    api = FakeAPI()                                     # no positions
    note, card = await commands.close_with_card(api, 42, "BTCUSDT")
    assert card is None and ("close_pos", 42, "BTCUSDT") in api.calls
