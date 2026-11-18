"""Named task presets: one ``preset`` key fills a create-task body with defaults.

Fields sent with the request always win over the preset's, so a preset is a
starting point, not a lock. ``podcast`` sets no instructions, which keeps its
tasks on the plain highlights path (same prompts and cache keys as before).
"""

from __future__ import annotations

from typing import Any, Dict, List

GTA_INSTRUCTIONS = (
    "This is a GTA gameplay stream with live commentary, mostly in Hinglish. "
    "Pick moments that are funny, chaotic or tense: police chases, crashes, heists, "
    "roleplay arguments and banter, unexpected fails and big reactions from the streamer. "
    "Each clip needs a clear setup and a payoff and must make sense without the rest of the stream. "
    "Skip menus, loading screens, setup talk, donation and chat reading, and long silent driving."
)

PRESETS: Dict[str, Dict[str, Any]] = {
    "podcast": {
        "name": "Podcast",
        "description": "Talking-head podcasts: face-tracked 9:16 crop, word-by-word captions, 25-50s clips.",
        "options": {
            "output_format": "vertical",
            "caption_template": "default",
            "add_subtitles": True,
            "include_broll": False,
            "clip_length": "auto",
        },
    },
    "gta": {
        "name": "GTA",
        "description": "GTA streams with commentary: facecam on top, gameplay below, bold captions, 18-25s clips.",
        "options": {
            "output_format": "gameplay_facecam",
            "caption_template": "mrbeast",
            "add_subtitles": True,
            "include_broll": False,
            "clip_length": "short",
            "instructions": GTA_INSTRUCTIONS,
        },
    },
}


class UnknownPreset(ValueError):
    pass


def apply_preset(data: Dict[str, Any]) -> Dict[str, Any]:
    """The create body with ``data["preset"]``'s defaults under the sent fields."""
    name = data.get("preset")
    if name is None:
        return data
    preset = PRESETS.get(str(name).strip().lower()) if isinstance(name, str) else None
    if preset is None:
        raise UnknownPreset(f"Unknown preset {name!r}; choose one of: {', '.join(PRESETS)}")
    merged = {**preset["options"], **data}
    if isinstance(preset["options"].get("font_options"), dict) and isinstance(data.get("font_options"), dict):
        merged["font_options"] = {**preset["options"]["font_options"], **data["font_options"]}
    return merged


def list_presets() -> List[Dict[str, Any]]:
    return [
        {"id": key, "name": preset["name"], "description": preset["description"], "options": dict(preset["options"])}
        for key, preset in PRESETS.items()
    ]
