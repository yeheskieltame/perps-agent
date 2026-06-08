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


def test_detail_mirror_writes_atomically(tmp_path):
    """Two puts both persist; the target stays valid JSON and no .tmp is left behind."""
    import json
    from pathlib import Path

    p = Path(tmp_path / "m.json")
    reg = RegimeFingerprint(0.2, 0.0, 0.0001, 0.02, 0.0)
    m = _DetailMirror(str(p))
    ch1, ch2 = config_hash(_gc("99")).hex(), config_hash(_gc("98")).hex()
    m.put(ch1, _gc("99"), reg)
    m.put(ch2, _gc("98"), reg)               # rewrite over the existing file
    assert json.loads(p.read_text()).keys() >= {ch1, ch2}   # valid JSON, both present
    assert not (tmp_path / "m.json.tmp").exists()           # temp renamed away, not left
    assert _DetailMirror(str(p)).get(ch1) is not None and _DetailMirror(str(p)).get(ch2) is not None
