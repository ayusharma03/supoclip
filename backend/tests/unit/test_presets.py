import pytest

from src.clip_duration import CLIP_DURATIONS
from src.media.common import VALID_OUTPUT_FORMATS
from src.caption_templates import CAPTION_TEMPLATES
from src.presets import PRESETS, UnknownPreset, apply_preset, list_presets


def test_no_preset_leaves_the_body_alone():
    body = {"source": {"url": "u"}}
    assert apply_preset(body) is body


def test_gta_preset_fills_its_defaults():
    body = apply_preset({"source": {"url": "u"}, "preset": "gta"})
    assert body["output_format"] == "gameplay_facecam"
    assert body["clip_length"] == "short"
    assert body["caption_template"] == "mrbeast"
    assert "GTA" in body["instructions"]
    assert body["source"] == {"url": "u"}


def test_sent_fields_win_over_the_preset():
    body = apply_preset({"source": {"url": "u"}, "preset": "GTA", "clip_length": "medium", "instructions": "x"})
    assert body["clip_length"] == "medium"
    assert body["instructions"] == "x"


def test_podcast_preset_sets_no_instructions():
    # Keeps podcast tasks on the plain highlights path (unchanged prompts and cache keys).
    assert "instructions" not in apply_preset({"preset": "podcast"})


def test_unknown_preset_is_rejected():
    with pytest.raises(UnknownPreset):
        apply_preset({"preset": "minecraft"})


@pytest.mark.parametrize("key", list(PRESETS))
def test_every_preset_uses_real_settings(key):
    options = PRESETS[key]["options"]
    assert options["output_format"] in VALID_OUTPUT_FORMATS
    assert options["clip_length"] in CLIP_DURATIONS
    assert options["caption_template"] in CAPTION_TEMPLATES


def test_list_presets_names_both():
    assert [p["id"] for p in list_presets()] == ["podcast", "gta"]
