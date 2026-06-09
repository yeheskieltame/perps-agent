"""Settings.assert_consistent() — the env<->venue guard that runs before any
money path. Hermetic: _env_file=None so the real backend/.env never leaks in."""
import pytest

from perpsagent.config import Settings

SEPOLIA = "https://rpc.sepolia.mantle.xyz"
MAINNET_RPC = "https://rpc.mantle.xyz"


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_default_testnet_ok():
    _settings(env="testnet", bybit_testnet=True, mantle_rpc=SEPOLIA).assert_consistent()


def test_testnet_with_mainnet_rpc_refused():
    with pytest.raises(ValueError, match="does not look like testnet"):
        _settings(env="testnet", bybit_testnet=True, mantle_rpc=MAINNET_RPC).assert_consistent()


def test_mainnet_with_bybit_testnet_refused():
    with pytest.raises(ValueError, match="bybit_testnet"):
        _settings(env="mainnet", bybit_testnet=True, mantle_rpc=MAINNET_RPC).assert_consistent()


def test_testnet_with_real_bybit_refused():
    """The inverse mismatch: real Bybit money under a testnet label."""
    with pytest.raises(ValueError, match="real Bybit money"):
        _settings(env="testnet", bybit_testnet=False, mantle_rpc=SEPOLIA).assert_consistent()


def test_mainnet_with_sepolia_and_no_optin_refused():
    """Real Bybit money but a testnet Mantle RPC, without the explicit opt-in:
    refuse, and point the user at the flag."""
    with pytest.raises(ValueError, match="PERPSAGENT_PROOFS_ON_TESTNET"):
        _settings(env="mainnet", bybit_testnet=False, mantle_rpc=SEPOLIA).assert_consistent()


def test_mainnet_bybit_with_proofs_on_testnet_ok():
    """The chosen mode: real money on Bybit, proofs stay on free Mantle Sepolia."""
    _settings(env="mainnet", bybit_testnet=False, proofs_on_testnet=True,
              mantle_rpc=SEPOLIA).assert_consistent()


def test_full_mainnet_ok():
    _settings(env="mainnet", bybit_testnet=False, mantle_rpc=MAINNET_RPC).assert_consistent()


def test_proofs_on_testnet_with_mainnet_rpc_refused():
    """opt-in claims testnet proofs but the RPC is a real chain — contradiction."""
    with pytest.raises(ValueError, match="not a testnet"):
        _settings(env="mainnet", bybit_testnet=False, proofs_on_testnet=True,
                  mantle_rpc=MAINNET_RPC).assert_consistent()


def test_proofs_on_testnet_only_valid_on_mainnet():
    with pytest.raises(ValueError, match="only applies when env=mainnet"):
        _settings(env="testnet", bybit_testnet=True, proofs_on_testnet=True,
                  mantle_rpc=SEPOLIA).assert_consistent()
