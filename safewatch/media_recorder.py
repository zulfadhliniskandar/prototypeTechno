"""Incident photo and H.264 MP4 video clip recorder for SafeWatch."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np

from .config import CAPTURES_DIR


def _find_ffmpeg() -> str | None:
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    playwright_root = Path.home() / "AppData" / "Local" / "ms-playwright"
    if playwright_root.exists():
        for exe in playwright_root.rglob("ffmpeg*.exe"):
            return str(exe)
    return None


def save_incident_photo(
    frame: np.ndarray,
    event_id: str,
    captures_dir: Path = CAPTURES_DIR,
) -> str:
    """Save incident photo as JPEG and return filename."""
    captures_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{event_id}.jpg"
    out_path = captures_dir / filename
    cv2.imwrite(str(out_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return filename


def save_incident_video(
    frames: list[tuple[float, np.ndarray]],
    event_id: str,
    captures_dir: Path = CAPTURES_DIR,
) -> tuple[str | None, float]:
    """
    Encode buffered frames into a browser-compatible H.264 MP4 video clip.
    Returns (filename_or_None, duration_seconds).
    """
    if not frames:
        return None, 0.0

    captures_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{event_id}.mp4"
    out_path = captures_dir / filename

    # Estimate actual FPS from frame timestamps
    if len(frames) >= 2:
        elapsed = max(frames[-1][0] - frames[0][0], 0.5)
        fps = min(max(len(frames) / elapsed, 4.0), 25.0)
    else:
        elapsed = 1.0
        fps = 8.0

    first_h, first_w = frames[0][1].shape[:2]
    # H.264 requires even dimensions
    target_w = max(first_w - (first_w % 2), 64)
    target_h = max(first_h - (first_h % 2), 64)

    def _norm(img: np.ndarray) -> np.ndarray:
        if img.shape[1] != target_w or img.shape[0] != target_h:
            return cv2.resize(img, (target_w, target_h))
        return img

    # Prefer Windows Media Foundation H.264 (browser-native playback)
    try:
        writer = cv2.VideoWriter(
            str(out_path),
            cv2.CAP_MSMF,
            cv2.VideoWriter_fourcc(*"H264"),
            fps,
            (target_w, target_h),
        )
        if writer.isOpened():
            for _, frm in frames:
                writer.write(_norm(frm))
            writer.release()
            if out_path.exists() and out_path.stat().st_size > 512:
                return filename, round(elapsed, 1)
    except Exception:
        pass

    # Fallback: write MJPG AVI and transcode with ffmpeg to H.264 MP4
    ffmpeg_bin = _find_ffmpeg()
    tmp_avi = captures_dir / f"{event_id}_tmp.avi"
    try:
        avi_writer = cv2.VideoWriter(
            str(tmp_avi),
            cv2.VideoWriter_fourcc(*"MJPG"),
            fps,
            (target_w, target_h),
        )
        if avi_writer.isOpened():
            for _, frm in frames:
                avi_writer.write(_norm(frm))
            avi_writer.release()
            if ffmpeg_bin and tmp_avi.exists():
                subprocess.run(
                    [
                        ffmpeg_bin,
                        "-y",
                        "-i",
                        str(tmp_avi),
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        "-movflags",
                        "+faststart",
                        str(out_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                tmp_avi.unlink(missing_ok=True)
                if out_path.exists() and out_path.stat().st_size > 512:
                    return filename, round(elapsed, 1)
    except Exception:
        tmp_avi.unlink(missing_ok=True)

    return None, round(elapsed, 1)
