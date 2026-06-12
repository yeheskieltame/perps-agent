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

    async def stop(self, uid, iid):
        self.calls.append(("stop", uid, iid))
        self._maybe_fail()

    async def pause(self, uid, iid):
        self.calls.append(("pause", uid, iid))
        self._maybe_fail()

    async def market(self, uid, market):
        self._maybe_fail()
        return {"market": market, "bid": "99.9", "ask": "100.1", "mid": "100.0"}

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
    assert "Your strategy settings" in await commands.set_value(api, 42, "")
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
