"""Allowlist semantics + env parsing."""
from perpsbot.config import BotSettings, is_allowed


def test_allowlist_empty_is_open():
    assert is_allowed(123, set())
    assert is_allowed(None, set())


def test_allowlist_enforced():
    allow = {1, 2}
    assert is_allowed(1, allow)
    assert not is_allowed(3, allow)
    assert not is_allowed(None, allow)


def test_settings_parse_allowlist():
    s = BotSettings(_env_file=None, allowlist="11, 22,33")
    assert s.allowed_ids() == {11, 22, 33}
    assert BotSettings(_env_file=None).allowed_ids() == set()
