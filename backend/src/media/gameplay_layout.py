"""Gameplay + facecam stack for streamed games (output format ``gameplay_facecam``).

A stream is a landscape game frame with the streamer's webcam overlaid in a
corner. The 9:16 render stacks a crop of that webcam on top of the gameplay:

    +-----------------+  0
    |     facecam     |  FACECAM_H
    +-----------------+
    |                 |
    |    gameplay     |  GAMEPLAY_H, centre crop of the full game frame
    |                 |
    +-----------------+  1920

A clip without a steady facecam gets the full frame fitted on a blurred
background instead, so nothing is cut off.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from .common import UPSCALE_SHARPEN_FILTER, logger
from .ffmpeg import clamp_even, ffprobe_duration, round_to_even

OUTPUT_W = 1080
FACECAM_H = 768
GAMEPLAY_H = 1920 - FACECAM_H

# A webcam overlay shows a small face that barely moves. In-game faces are
# rare, short-lived and move around, so they fail these checks.
MIN_DETECTIONS = 4
MAX_FACE_AREA_FRACTION = 0.06  # bigger faces mean a full-screen camera, not an overlay
MAX_CENTER_SPREAD_FRACTION = 0.06  # of the frame width
FACE_BOX_SCALE = 3.4  # facecam crop width per face width
PROBE_SECONDS = 12.0

Face = Tuple[int, int, int, float]


def facecam_region(faces: List[Face], width: int, height: int) -> Optional[Dict[str, int]]:
    """Crop box (x, y, w, h) around a steady small face, or None."""
    if len(faces) < MIN_DETECTIONS or width <= 0 or height <= 0:
        return None
    xs = np.array([face[0] for face in faces], dtype=float)
    ys = np.array([face[1] for face in faces], dtype=float)
    cx, cy = float(np.median(xs)), float(np.median(ys))
    # Keep the detections at the steady spot; stray in-game faces drop out.
    near = [
        face for face in faces
        if abs(face[0] - cx) <= width * MAX_CENTER_SPREAD_FRACTION
        and abs(face[1] - cy) <= width * MAX_CENTER_SPREAD_FRACTION
    ]
    if len(near) < MIN_DETECTIONS or len(near) < len(faces) * 0.6:
        return None
    area = float(np.median([face[2] for face in near]))
    if area > width * height * MAX_FACE_AREA_FRACTION:
        return None

    face_w = float(np.sqrt(max(area, 1.0)))
    crop_w = min(width, max(160.0, face_w * FACE_BOX_SCALE))
    crop_h = crop_w * FACECAM_H / OUTPUT_W
    if crop_h > height:
        crop_h = float(height)
        crop_w = crop_h * OUTPUT_W / FACECAM_H
    crop_w, crop_h = round_to_even(int(crop_w)), round_to_even(int(crop_h))
    # Faces sit a little above the middle of a webcam frame.
    x = clamp_even(int(cx - crop_w / 2), 0, max(0, width - crop_w))
    y = clamp_even(int(cy - crop_h * 0.45), 0, max(0, height - crop_h))
    return {"x": x, "y": y, "w": crop_w, "h": crop_h}


def gameplay_crop(width: int, height: int) -> Dict[str, int]:
    """Centre crop of the game frame with the gameplay panel's aspect ratio."""
    crop_h = height
    crop_w = round_to_even(min(width, int(height * OUTPUT_W / GAMEPLAY_H)))
    if crop_w == width:
        crop_h = round_to_even(min(height, int(width * GAMEPLAY_H / OUTPUT_W)))
    return {
        "x": clamp_even((width - crop_w) // 2, 0, max(0, width - crop_w)),
        "y": clamp_even((height - crop_h) // 2, 0, max(0, height - crop_h)),
        "w": crop_w,
        "h": crop_h,
    }


def build_gameplay_facecam_filter(region: Dict[str, int], width: int, height: int) -> str:
    """filter_complex producing ``[vout]``: facecam on top, gameplay below."""
    game = gameplay_crop(width, height)
    return (
        "[0:v]split=2[cam][game];"
        f"[cam]crop={region['w']}:{region['h']}:{region['x']}:{region['y']},"
        f"scale={OUTPUT_W}:{FACECAM_H}:flags=lanczos,{UPSCALE_SHARPEN_FILTER},setsar=1[top];"
        f"[game]crop={game['w']}:{game['h']}:{game['x']}:{game['y']},"
        f"scale={OUTPUT_W}:{GAMEPLAY_H}:flags=lanczos,setsar=1[bottom];"
        "[top][bottom]vstack,setsar=1[vout]"
    )


def detect_facecam_region(clip_path: Path, width: int, height: int) -> Optional[Dict[str, int]]:
    from .reframing import detect_faces_in_clip

    try:
        duration = ffprobe_duration(clip_path)
        faces = detect_faces_in_clip(clip_path, 0, min(duration, PROBE_SECONDS))
    except Exception:
        logger.warning("Facecam detection failed for %s", clip_path, exc_info=True)
        return None
    return facecam_region(faces, width, height)


def gameplay_facecam_plan(clip_path: Path, width: int, height: int) -> Tuple[str, str]:
    """(filter_complex, "complex") for the stack, or the blurred full-frame fit."""
    from .reframing import build_full_frame_filter

    region = detect_facecam_region(clip_path, width, height)
    if region is None:
        logger.info("No steady facecam in %s; fitting the full game frame", clip_path.name)
        return build_full_frame_filter(), "complex"
    logger.info("Facecam at %s in %s", region, clip_path.name)
    return build_gameplay_facecam_filter(region, width, height), "complex"
