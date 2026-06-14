"""Command logic against a fake API — every user-facing path, offline."""

from perpsbot.api import ApiError
from perpsbot import commands


# Mirrors the backend's GET /v1/settings payload (validation lives backend-side;
# the bot only renders what it gets).
DEFAULTS = {"band": "1", "levels": "10", "size": "0.001", "leverage": "1",
            "max_inventory": "0", "max_drawdown": "0", "account_dd": "0",
            "tp": "0", "trail": "0", "trail_arm": "0",
            "bias": "neutral", "timeframe": "1m", "recenter": "auto"}


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.fail: ApiError | None = None
        self.rows = []
        self.settings = dict(DEFAULTS)
        self.customized: list[str] = []

    def _maybe_fail(self):
        if self.fail:
            raise self.fail

    async def create_grid(self, uid, market, settings=None):
        self.calls.append(("create", uid, market, settings))
        self._maybe_fail()
        # the real backend normalizes aliases before merging (app/prefs.py)
        aliases = {"lev": "leverage", "tf": "timeframe"}
        normalized = {aliases.get(k, k): v for k, v in (settings or {}).items()}
        return {"instance_id": f"{market}-0-abc123",
                "effective": {**self.settings, **normalized},
                "lower": "99.0", "upper": "101.0"}

    async def get_settings(self, uid):
        self._maybe_fail()
        return {"settings": dict(self.settings), "customized": list(self.customized)}

    async def put_settings(self, uid, updates):
        self.calls.append(("put_settings", uid, updates))
        self._maybe_fail()
        self.settings.update(updates)
        self.customized = sorted(set(self.customized) | set(updates))
        return {"settings": dict(self.settings), "updated": sorted(updates)}

    async def reset_settings(self, uid):
        self.calls.append(("reset_settings", uid))
        self._maybe_fail()
        self.settings = dict(DEFAULTS)
        self.customized = []
        return {"settings": dict(self.settings)}

    async def status(self, uid):
        self._maybe_fail()
        return self.rows

    async def clear_stopped(self, uid):
        self._maybe_fail()
        return {"cleared": getattr(self, "cleared", 0)}

    async def history(self, uid):
        self._maybe_fail()
        return getattr(self, "hist", [])

    async def stop(self, uid, iid):
        self.calls.append(("stop", uid, iid))
        self._maybe_fail()

    async def pause(self, uid, iid):
        self.calls.append(("pause", uid, iid))
        self._maybe_fail()

    async def market(self, uid, market):
        self._maybe_fail()
        return {"market": market, "bid": "99.9", "ask": "100.1", "mid": "100.0"}

    async def wallet(self, uid):
        self._maybe_fail()
        return {"address": "0x" + "ab" * 20, "balance": "1500000000000000000",
                "currency": "MNT", "faucet": "https://faucet.sepolia.mantle.xyz"}

    async def balance(self, uid):
        self._maybe_fail()
        return {"equity": "73193.62", "available": "73000.00", "currency": "USDT"}

    async def health(self):
        self._maybe_fail()
        return {"ok": True, "node": "0"}

    async def put_credentials(self, uid, api_key, api_secret, testnet=True):
        self.calls.append(("put_creds", uid, api_key, api_secret, testnet))
        self._maybe_fail()
        return {"ok": True, "testnet": testnet}

    async def get_credentials(self, uid):
        self._maybe_fail()
        return self.creds if hasattr(self, "creds") else {"connected": False}

    async def delete_credentials(self, uid):
        self.calls.append(("del_creds", uid))
        self._maybe_fail()


async def test_grid_no_args_uses_saved_settings():
    api = FakeAPI()
    out = await commands.grid(api, 42, "btcusdt")
    assert api.calls == [("create", 42, "BTCUSDT", None)]   # backend merges saved
    assert "Grid launched" in out and "BTCUSDT-0-abc123" in out
    assert "[99.0, 101.0]" in out and "10 levels" in out and "lev 1x" in out


async def test_grid_positional_args_still_work():
    api = FakeAPI()
    out = await commands.grid(api, 42, "ETHUSDT 0.8 12 0.002")
    assert api.calls == [("create", 42, "ETHUSDT",
                          {"band": "0.8", "levels": "12", "size": "0.002"})]
    assert "12 levels" in out and "0.002/level" in out


async def test_grid_key_value_overrides():
    api = FakeAPI()
    out = await commands.grid(api, 42, "MNTUSDT band=1.5 lev=25 tp=0.5")
    assert api.calls == [("create", 42, "MNTUSDT",
                          {"band": "1.5", "lev": "25", "tp": "0.5"})]
    assert "lev 25x" in out and "tp 0.5" in out


async def test_grid_rejects_malformed_shape_without_calling_api():
    api = FakeAPI()
    assert "Usage" in await commands.grid(api, 42, "")
    assert "parse" in await commands.grid(api, 42, "BTCUSDT band=")      # empty value
    assert "Unexpected" in await commands.grid(api, 42, "BTCUSDT 1 10 0.001 extra")
    assert api.calls == []   # nothing reached the backend


async def test_grid_shows_backend_validation_verbatim():
    api = FakeAPI()
    api.fail = ApiError(400, "band (percent, e.g. 1 = ±1%): must be in [0.05, 10], got 15")
    out = await commands.grid(api, 42, "BTCUSDT band=15")
    assert "must be in [0.05, 10]" in out and "❌" in out


async def test_grid_surfaces_backend_refusal():
    api = FakeAPI()
    api.fail = ApiError(409, "user 42 is not on shard 0")
    out = await commands.grid(api, 42, "BTCUSDT")
    assert "409" in out and "shard" in out


async def test_settings_card_groups_and_marks_custom():
    api = FakeAPI()
    api.settings["leverage"] = "25"
    api.customized = ["leverage"]
    out = await commands.settings_show(api, 42)
    for group in ("Grid shape", "Risk", "Exit", "Behavior"):
        assert group in out
    assert "leverage = 25</code> ✏️" in out and "/set KEY VALUE" in out


async def test_set_saves_one_key_and_reset_restores():
    api = FakeAPI()
    out = await commands.set_value(api, 42, "leverage 25")
    assert ("put_settings", 42, {"leverage": "25"}) in api.calls
    assert "Saved" in out and "leverage = 25" in out and "NEW grid" in out
    out = await commands.set_value(api, 42, "tf=1h")          # KEY=VALUE form too
    assert ("put_settings", 42, {"tf": "1h"}) in api.calls
    out = await commands.set_value(api, 42, "reset")
    assert ("reset_settings", 42) in api.calls
    assert "reset" in out.lower() and api.settings == DEFAULTS


async def test_set_without_args_shows_card_and_bad_value_is_friendly():
    api = FakeAPI()
    assert "Config" in await commands.set_value(api, 42, "")
    assert "Usage" in await commands.set_value(api, 42, "leverage")      # value missing
    api.fail = ApiError(400, "unknown setting 'banana' — valid: band, levels, ...")
    out = await commands.set_value(api, 42, "banana 1")
    assert "unknown setting" in out and "400" not in out                 # friendly, not raw


async def test_status_empty_and_rows():
    api = FakeAPI()
    assert "No grids yet" in await commands.status(api, 42)
    api.rows = [{"instance_id": "i1", "state": "RUNNING", "realized_pnl": "0.236", "fill_count": 12}]
    out = await commands.status(api, 42)
    assert "i1" in out and "RUNNING" in out and "0.236" in out and "12" in out


async def test_stop_and_pause():
    api = FakeAPI()
    assert "Usage" in await commands.stop(api, 42, "")
    assert "Stopped" in await commands.stop(api, 42, "i1")
    assert "Paused" in await commands.pause(api, 42, "i1")
    assert ("stop", 42, "i1") in api.calls and ("pause", 42, "i1") in api.calls


async def test_price_balance_health():
    api = FakeAPI()
    assert "mid 100.0" in await commands.price(api, 42, "btcusdt")
    assert "Usage" in await commands.price(api, 42, "")
    assert "73193.62 USDT" in await commands.balance(api, 42)
    assert "ok" in await commands.health(api)


async def test_health_backend_down():
    class DeadAPI:
        async def health(self):
            raise ConnectionError("connection refused")

    assert "unreachable" in await commands.health(DeadAPI())


def test_parse_env_choice_is_strict():
    assert commands.parse_env_choice("testnet") is True
    assert commands.parse_env_choice("  MAINNET ") is False
    # anything ambiguous re-asks — mainnet must never come from a typo
    for bad in ("", "main", "yes", "test net", "mainnet please"):
        assert commands.parse_env_choice(bad) is None


async def test_connect_start_shows_existing_link():
    api = FakeAPI()
    assert "API key" in await commands.connect_start(api, 42)        # fresh
    api.creds = {"connected": True, "testnet": True, "key_preview": "cR8x…"}
    out = await commands.connect_start(api, 42)
    assert "Already connected" in out and "cR8x…" in out and "overwrite" in out


async def test_connect_finish_and_disconnect():
    api = FakeAPI()
    out = await commands.connect_finish(api, 42, "k123", "s456", testnet=True)
    assert ("put_creds", 42, "k123", "s456", True) in api.calls
    assert "Connected" in out and "testnet" in out
    out = await commands.connect_finish(api, 42, "k123", "s456", testnet=False)
    assert "MAINNET" in out and "real money" in out.lower()
    assert "Keys forgotten" in await commands.disconnect(api, 42)
    assert ("del_creds", 42) in api.calls


async def test_401_points_to_connect():
    api = FakeAPI()
    api.fail = ApiError(401, "no venue credentials — connect your API keys first")
    out = await commands.grid(api, 42, "BTCUSDT")
    assert "/connect" in out and "401" not in out                    # friendly, not raw


class ProofAPI(FakeAPI):
    """Backend with the verifiable loop on-chain — returns commit/attest tx hashes."""

    async def create_grid(self, uid, market, settings=None):
        resp = await super().create_grid(uid, market, settings)
        resp["proofs"] = {"commit": "0x" + "ab" * 32}
        return resp

    async def stop(self, uid, iid):
        await super().stop(uid, iid)
        return {"ok": True, "proofs": {"attest": "0x" + "cd" * 32, "memory": "0x" + "ef" * 32}}


async def test_launch_shows_onchain_commit_link():
    api = ProofAPI()
    out = await commands.grid(api, 42, "BTCUSDT")
    assert "committed on-chain" in out
    assert "sepolia.mantlescan.xyz/tx/0x" + "ab" * 32 in out         # clickable proof

    note = await commands.launch_note(api, 42, "BTCUSDT")
    assert "committed on-chain" in note


async def test_stop_shows_onchain_attest_link():
    api = ProofAPI()
    out = await commands.stop(api, 42, "BTCUSDT-0-abc123")
    assert "attested on-chain" in out and "/tx/0x" + "cd" * 32 in out


async def test_proofs_absent_when_chain_disabled():
    api = FakeAPI()                                                   # no proofs in payload
    out = await commands.grid(api, 42, "BTCUSDT")
    assert "on-chain" not in out                                     # silent, not broken


async def test_wallet_shows_address_and_mnt_balance():
    out = await commands.wallet(FakeAPI(), 42)
    assert "0x" + "ab" * 20 in out and "1.5000 MNT" in out


async def test_topup_shows_faucet_and_address():
    out = await commands.topup(FakeAPI(), 42)
    assert "faucet.sepolia.mantle.xyz" in out and "0x" + "ab" * 20 in out


async def test_wallet_unavailable_is_friendly():
    api = FakeAPI()
    api.fail = ApiError(503, "wallet storage not configured")
    out = await commands.wallet(api, 42)
    assert "unavailable" in out.lower() and "503" not in out


# ── one-screen home + wizard renderers (PR #48 UI over this branch's logic) ──

async def test_menu_snapshot_not_connected_shows_wallet_and_empty_positions():
    out = await commands.menu_snapshot(FakeAPI(), 42)
    assert "Not connected" in out
    assert "MNT wallet" in out and "1.5000 MNT" in out
    assert "No grids running" in out


async def test_menu_snapshot_connected_lists_positions_and_equity():
    api = FakeAPI()
    api.creds = {"connected": True, "testnet": True, "key_preview": "cR8x…"}
    api.rows = [{"instance_id": "BTCUSDT-0-ab", "state": "RUNNING",
                 "realized_pnl": "0", "fill_count": 3}]
    out = await commands.menu_snapshot(api, 42)
    assert "Bybit testnet" in out and "73193.62" in out
    assert "Positions" in out and "BTCUSDT-0-ab" in out


def test_render_status_empty_and_rows():
    assert "No grids" in commands.render_status([])
    out = commands.render_status([{"instance_id": "i1", "state": "RUNNING",
                                   "realized_pnl": "1.2", "fill_count": 5}])
    assert "i1" in out and "RUNNING" in out


def test_grid_confirm_text_shows_percent_and_style():
    out = commands.grid_confirm_text("BTCUSDT", "0.01", 10, "0.001", style="balanced")
    assert "BTCUSDT" in out and "±1%" in out and "Steps: <b>10</b>" in out
    assert "Balanced" in out                                  # named style in the header
    assert "Custom" in commands.grid_confirm_text("BTCUSDT", "0.01", 10, "0.001")


async def test_create_grid_result_converts_band_to_percent_and_shows_commit():
    api = ProofAPI()
    text, iid = await commands.create_grid_result(api, 42, "btcusdt", "0.015", 12, "0.002")
    assert iid == "BTCUSDT-0-abc123"
    assert "Grid launched" in text and "committed on-chain" in text
    create = [c for c in api.calls if c[0] == "create"][-1]
    assert create[3]["band"] == "1.5"          # fraction 0.015 → percent 1.5 for the knob


async def test_create_grid_result_surfaces_validation_error():
    api = FakeAPI()
    api.fail = ApiError(400, "band: must be in [0.05, 10]")
    text, iid = await commands.create_grid_result(api, 42, "BTCUSDT", "0.0001", 10, "0.001")
    assert iid is None and "band" in text


def test_render_history_empty_and_rows():
    assert "No closed grids" in commands.render_history([])
    out = commands.render_history([{"instance_id": "i1", "market": "BTCUSDT",
                                    "realized_pnl": "3.4", "fill_count": 42, "winrate": 1.0}])
    assert "i1" in out and "BTCUSDT" in out and "3.4" in out and "win 100%" in out


async def test_history_command_renders():
    api = FakeAPI()
    api.hist = [{"instance_id": "i1", "market": "BTCUSDT", "realized_pnl": "3.4",
                 "fill_count": 42, "winrate": 0.5}]
    out = await commands.history(api, 42)
    assert "i1" in out and "win 50%" in out


async def test_clear_stopped_note():
    api = FakeAPI()
    api.cleared = 2
    assert "Cleared 2" in await commands.clear_stopped(api, 42)
    assert "Nothing to clear" in await commands.clear_stopped(FakeAPI(), 42)


async def test_set_setting_applies_by_tap():
    api = FakeAPI()
    toast, new = await commands.set_setting(api, 42, "leverage", "10")
    assert "leverage → 10" in toast and new is not None and new["leverage"] == "10"


async def test_set_setting_invalid_is_a_toast_not_a_crash():
    api = FakeAPI()
    api.fail = ApiError(400, "leverage: must be in [1, 100]")
    toast, new = await commands.set_setting(api, 42, "leverage", "999")
    assert new is None and "leverage" in toast
