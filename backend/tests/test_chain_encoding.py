"""Unit tests for MantleChainClient encoding/decoding (no chain needed)."""
from decimal import Decimal

from perpsagent.adapters.chain.client import _DetailMirror, _b32, _bps, _clip_i32, config_hash
from perpsagent.domain.models import GridConfig, RegimeFingerprint, Spacing, Venue
from perpsagent.domain.regime import regime_key


def _gc(lower="99"):
    return GridConfig("i1", Venue.MANTLE_DEX, "BTCUSDT", Decimal(lower), Decimal("101"), 10,
                      Decimal("0.01"), Spacing.GEOMETRIC)


def test_config_hash_deterministic_and_32_bytes():
    a, b = config_hash(_gc()), config_hash(_gc())
    assert a == b and len(a) == 32
    assert config_hash(_gc(lower="98")) != a  # different params -> different hash


def test_bps_and_clip_i32():
    assert _bps(0.66) == 6600
    assert _bps(0.8333) == 8333
    assert _clip_i32(10**12) == 2**31 - 1
    assert _clip_i32(-(10**12)) == -(2**31)


def test_b32_is_32_bytes():
    assert len(_b32(regime_key(RegimeFingerprint(0.2, 0.0, 0.0001, 0.02, 0.0)))) == 32
    assert len(_b32("0x" + "ab" * 32)) == 32


def test_detail_mirror_roundtrip(tmp_path):
    path = str(tmp_path / "m.json")
    cfg, reg = _gc(), RegimeFingerprint(0.2, 0.0, 0.0001, 0.02, 0.0)
    ch = config_hash(cfg).hex()
    _DetailMirror(path).put(ch, cfg, reg)
    got = _DetailMirror(path).get(ch)  # reload from disk
    assert got is not None
    cfg2, reg2 = got
    assert cfg2.market == "BTCUSDT" and cfg2.levels == 10 and cfg2.lower == Decimal("99")
    assert cfg2.spacing is Spacing.GEOMETRIC and reg2.realized_vol == 0.2
