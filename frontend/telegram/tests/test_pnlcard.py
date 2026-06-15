"""PnL share-card: the pure PNG renderer, the position/grid builders, and the
close→snapshot flow."""
from perpsbot import commands, pnlcard
from tests.test_commands import FakeAPI

_PNG = b"\x89PNG\r\n\x1a\n"


def test_render_returns_a_png_for_win_loss_and_live():
    win = pnlcard.render({"title": "MNTUSDT", "subtitle": "LONG", "pnl": "0.86", "pnl_pct": "2.34",
                          "lines": ["Qty 250 MNT", "Entry 0.5691 → Exit 0.5705"]})
    assert win[:8] == _PNG and len(win) > 1000
    loss = pnlcard.render({"title": "BTCUSDT", "subtitle": "SHORT", "badge": "LIVE",
                           "pnl": "-5", "pnl_pct": "-0.77", "lines": ["Qty 0.01 BTC"]})
    assert loss[:8] == _PNG


def test_position_card_live_vs_close():
    p = {"market": "MNTUSDT", "side": "LONG", "size": "250", "entry": "0.5691",
         "mark": "0.5705", "pnl": "0.86", "pnl_pct": "2.34"}
    live = commands.position_card(p, live=True)
    assert live["badge"] == "LIVE" and live["title"] == "MNTUSDT"
    assert "250 MNT" in live["lines"][0] and "Mark" in live["lines"][1]
    closed = commands.position_card(p, live=False)
    assert closed["badge"] is None and "Exit" in closed["lines"][1]
    assert pnlcard.render(live)[:8] == _PNG


def test_grid_card_shows_realized_and_unrealized():
    d = {"name": "BTC scalp", "market": "BTCUSDT", "total_pnl": "11.0", "pnl_pct": "2.5",
         "realized_pnl": "12.5", "unrealized_pnl": "-1.5", "fill_count": 18}
    card = commands.grid_card(d, live=True)
    assert card["title"] == "BTC scalp" and card["subtitle"] == "GRID" and card["badge"] == "LIVE"
    assert "18 fills" in card["lines"][0] and "Realized +12.50" in card["lines"][1]
    assert pnlcard.render(card)[:8] == _PNG


def test_pnl_caption_shows_signed_pnl_percent_and_badge():
    cap = commands.pnl_caption({"title": "MNTUSDT", "subtitle": "LONG", "badge": "LIVE",
                                "pnl": "0.86", "pnl_pct": "2.34", "currency": "USDT"})
    assert "+0.86 USDT" in cap and "(+2.34%)" in cap and "LIVE" in cap and "🟢" in cap


async def test_close_with_card_snapshots_pnl_then_closes():
    api = FakeAPI()
    api.pos = [{"market": "MNTUSDT", "side": "LONG", "size": "250", "entry": "0.5691",
                "mark": "0.5705", "pnl": "0.86", "pnl_pct": "2.34", "notional": "142"}]
    note, card = await commands.close_with_card(api, 42, "MNTUSDT")
    assert ("close_pos", 42, "MNTUSDT") in api.calls           # the position WAS closed
    assert card is not None and card["title"] == "MNTUSDT" and card["pnl"] == "0.86"
    assert pnlcard.render(card)[:8] == _PNG                     # the snapshot renders


async def test_close_with_card_no_position_returns_no_card():
    api = FakeAPI()                                            # no positions
    note, card = await commands.close_with_card(api, 42, "BTCUSDT")
    assert card is None and ("close_pos", 42, "BTCUSDT") in api.calls
