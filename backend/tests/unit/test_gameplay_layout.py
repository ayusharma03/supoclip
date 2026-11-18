import json
import shutil
import subprocess

import pytest

from src.media.common import VALID_OUTPUT_FORMATS
from src.media.gameplay_layout import (
    FACECAM_H,
    GAMEPLAY_H,
    OUTPUT_W,
    build_gameplay_facecam_filter,
    facecam_region,
    gameplay_crop,
)

W, H = 1920, 1080


def steady_cam(n=8, x=1700, y=880, area=70 * 70):
    return [(x + (i % 3), y - (i % 2), area, 0.9) for i in range(n)]


def test_gameplay_facecam_is_a_valid_output_format():
    assert "gameplay_facecam" in VALID_OUTPUT_FORMATS


def test_a_steady_small_corner_face_is_the_facecam():
    region = facecam_region(steady_cam(), W, H)
    assert region is not None
    assert region["w"] % 2 == 0 and region["h"] % 2 == 0
    # The crop has the facecam panel's aspect ratio and contains the face.
    assert abs(region["w"] / region["h"] - OUTPUT_W / FACECAM_H) < 0.02
    assert region["x"] <= 1700 <= region["x"] + region["w"]
    assert region["y"] <= 880 <= region["y"] + region["h"]
    assert region["x"] + region["w"] <= W and region["y"] + region["h"] <= H


def test_faces_that_move_around_are_in_game_not_a_facecam():
    roaming = [(200 + i * 220, 300 + (i % 2) * 400, 60 * 60, 0.8) for i in range(8)]
    assert facecam_region(roaming, W, H) is None


def test_a_full_screen_face_is_not_an_overlay():
    assert facecam_region(steady_cam(area=500 * 500), W, H) is None


def test_too_few_detections_fall_back():
    assert facecam_region(steady_cam(n=2), W, H) is None


def test_gameplay_crop_matches_the_gameplay_panel():
    crop = gameplay_crop(W, H)
    assert crop["h"] == H
    assert abs(crop["w"] / crop["h"] - OUTPUT_W / GAMEPLAY_H) < 0.01
    assert crop["x"] == (W - crop["w"]) // 2 // 2 * 2


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_the_stack_renders_a_1080x1920_frame(tmp_path):
    source = tmp_path / "stream.mp4"
    out = tmp_path / "short.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={W}x{H}:rate=30:duration=1",
         "-pix_fmt", "yuv420p", str(source)],
        check=True,
    )
    graph = build_gameplay_facecam_filter(facecam_region(steady_cam(), W, H), W, H)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-filter_complex", graph,
         "-map", "[vout]", "-pix_fmt", "yuv420p", str(out)],
        check=True,
    )
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "json", str(out)],
        check=True, capture_output=True, text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (1080, 1920)
