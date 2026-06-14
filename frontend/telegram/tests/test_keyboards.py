"""Config keyboards — tap-to-set value pickers (no typing), built pure/offline."""
from perpsbot import keyboards


def _labels(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


def test_leverage_presets_offer_the_expected_multipliers():
    levs = dict(keyboards.KNOB_PRESETS["leverage"])
    assert levs["x1"] == "1" and levs["x10"] == "10" and levs["x25"] == "25"


def test_picker_marks_current_value_and_offers_custom():
    flat = _labels(keyboards.setting_picker_kb("leverage", "10"))
    assert any("✅" in t and "x10" in t for t in flat)   # current is ticked
    assert any("x25" in t for t in flat)
    assert any("Custom" in t for t in flat) and any("Back" in t for t in flat)


def test_every_knob_has_a_label_and_plain_help():
    for key in keyboards.KNOB_PRESETS:
        assert key in keyboards.KNOB_LABEL
        assert keyboards.KNOB_HELP.get(key)            # picker explains what it means


def test_config_kb_one_button_per_knob_shows_value():
    flat = _labels(keyboards.config_kb({"leverage": "1", "band": "1"}))
    assert any("Leverage: 1" in t for t in flat)
    assert any("Range %: 1" in t for t in flat)
    assert any("Reset" in t for t in flat)
