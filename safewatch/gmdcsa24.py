"""GMDCSA-24 Fall Detection Dataset integration, parser, downloader, and evaluator.

Dataset Reference:
    Ekram Alam et al., "GMDCSA24: A Dataset for Human Fall Detection in Videos"
    Zenodo DOI: https://doi.org/10.5281/zenodo.13354453
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import requests

from .config import GMDCSA24_DIR, GMDCSA24_DOI, GMDCSA24_GITHUB_RAW, GMDCSA24_ZENODO_ZIP_URL
from .fall_detector import (
    STATE_FALL_DETECTED,
    CameraFallAnalyzer,
    PoseEngine,
)

# Representative sample subset from Subject 1 (Fall + ADL) for quick local verification
SAMPLE_FILES = [
    "Subject 1/Fall.csv",
    "Subject 1/ADL.csv",
    "Subject 1/Fall/01.mp4",
    "Subject 1/Fall/03.mp4",
    "Subject 1/Fall/05.mp4",
    "Subject 1/ADL/01.mp4",
    "Subject 1/ADL/05.mp4",
]

_CLASS_INTERVAL_RE = re.compile(r"([A-Za-z0-9 ()_-]+?)\[([\d.]+)\s*to\s*([\d.]+)\]")


@dataclass
class GMDCSAClipInfo:
    subject: str
    category: str  # "Fall" | "ADL"
    filename: str
    video_path: Path
    length_sec: float
    description: str
    classes_raw: str
    fall_intervals: list[tuple[float, float]] = field(default_factory=list)

    @property
    def is_fall_ground_truth(self) -> bool:
        return self.category.lower() == "fall" or len(self.fall_intervals) > 0


def parse_class_intervals(classes_str: str) -> list[tuple[str, float, float]]:
    out = []
    for part in classes_str.split(";"):
        m = _CLASS_INTERVAL_RE.search(part.strip())
        if m:
            label = m.group(1).strip()
            t0, t1 = float(m.group(2)), float(m.group(3))
            out.append((label, t0, t1))
    return out


def discover_dataset_clips(dataset_dir: Path = GMDCSA24_DIR) -> list[GMDCSAClipInfo]:
    """Scan local GMDCSA-24 directory for Subject folders, CSV annotations, and video clips."""
    clips: list[GMDCSAClipInfo] = []
    if not dataset_dir.exists():
        return clips

    for subject_dir in sorted(dataset_dir.iterdir()):
        if not subject_dir.is_dir():
            continue
        for category in ("Fall", "ADL"):
            csv_path = subject_dir / f"{category}.csv"
            vid_dir = subject_dir / category
            annotations: dict[str, dict[str, str]] = {}
            if csv_path.exists():
                with csv_path.open("r", encoding="utf-8", errors="ignore") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        clean = {k.strip(): (v or "").strip() for k, v in row.items() if k}
                        fn = clean.get("File Name", "")
                        if fn:
                            annotations[fn] = clean

            if not vid_dir.exists():
                continue
            for vp in sorted(vid_dir.glob("*.mp4")):
                meta = annotations.get(vp.name, {})
                classes_raw = meta.get("Classes", "")
                parsed = parse_class_intervals(classes_raw)
                fall_intervals = [
                    (t0, t1) for lbl, t0, t1 in parsed if "fall" in lbl.lower()
                ]
                try:
                    length_sec = float(meta.get("Length (seconds)", "0") or 0)
                except ValueError:
                    length_sec = 0.0
                clips.append(
                    GMDCSAClipInfo(
                        subject=subject_dir.name,
                        category=category,
                        filename=vp.name,
                        video_path=vp,
                        length_sec=length_sec,
                        description=meta.get("Description", ""),
                        classes_raw=classes_raw,
                        fall_intervals=fall_intervals,
                    )
                )
    return clips


def download_sample_clips(dest_dir: Path = GMDCSA24_DIR) -> list[Path]:
    """Download representative Fall & ADL sample clips and CSVs from GMDCSA-24."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []
    for rel in SAMPLE_FILES:
        out_path = dest_dir / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.exists() and out_path.stat().st_size > 100:
            downloaded.append(out_path)
            continue
        url = f"{GMDCSA24_GITHUB_RAW}/{rel.replace(' ', '%20')}"
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        out_path.write_bytes(resp.content)
        downloaded.append(out_path)
    return downloaded


def download_full_zenodo_dataset(dest_dir: Path = GMDCSA24_DIR) -> Path:
    """Download and extract the full GMDCSA-24 v2.1 archive (~1.05 GB) from Zenodo."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / "GMDCSA24-v2.1.zip"
    if not zip_path.exists():
        with requests.get(GMDCSA24_ZENODO_ZIP_URL, stream=True, timeout=120) as r:
            r.raise_for_status()
            with zip_path.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)
    return dest_dir


def evaluate_video_clip(
    video_path: Path,
    pose_engine: PoseEngine,
    sensitivity: str = "medium",
    sample_fps: float = 8.0,
) -> dict[str, Any]:
    """Run the 4-state pose fall detector across a video file using video timestamps."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    native_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    stride = max(int(round(native_fps / sample_fps)), 1)
    analyzer = CameraFallAnalyzer(pose_engine)
    cam_cfg = {
        "room": "GMDCSA-24 Eval",
        "ip_address": "dataset",
        "sensitivity": sensitivity,
        "fall_detection_enabled": True,
        "inactivity_enabled": False,
        "motion_enabled": False,
        "motion_assist_enabled": False,
    }

    states_sequence: list[str] = []
    fall_detected = False
    detection_time_sec: float | None = None
    max_conf = 0.0
    max_torso = 0.0
    max_body = 0.0
    keypoint_modes: set[str] = set()
    frame_idx = 0
    analyzed_frames = 0

    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if frame_idx % stride == 0:
                ts = frame_idx / native_fps
                res = analyzer.analyze(
                    frame,
                    cam_cfg,
                    cooldown_seconds=60.0,
                    draw_skeleton=False,
                    frame_ts=ts,
                )
                analyzed_frames += 1
                if not states_sequence or states_sequence[-1] != res.posture:
                    states_sequence.append(res.posture)
                if res.person_detected:
                    keypoint_modes.add(res.keypoint_source)
                    max_torso = max(max_torso, res.torso_angle)
                    max_body = max(max_body, res.body_angle)
                if res.fall_triggered or res.posture == STATE_FALL_DETECTED:
                    if not fall_detected:
                        detection_time_sec = round(ts, 2)
                    fall_detected = True
                    max_conf = max(max_conf, res.confidence)
            frame_idx += 1
    finally:
        cap.release()

    return {
        "analyzed_frames": analyzed_frames,
        "states_sequence": states_sequence,
        "fall_detected": fall_detected,
        "detection_time_sec": detection_time_sec,
        "max_confidence": round(max_conf, 1),
        "max_torso_angle": round(max_torso, 1),
        "max_body_angle": round(max_body, 1),
        "cancelled_count": analyzer.cancelled_count,
        "keypoint_sources": sorted(keypoint_modes),
    }


def evaluate_dataset(
    dataset_dir: Path = GMDCSA24_DIR,
    pose_engine: PoseEngine | None = None,
    sensitivity: str = "medium",
    max_clips: int | None = None,
) -> dict[str, Any]:
    """Evaluate all discovered GMDCSA-24 clips and compute detection metrics."""
    clips = discover_dataset_clips(dataset_dir)
    if max_clips:
        clips = clips[:max_clips]
    engine = pose_engine or PoseEngine()

    tp = fp = tn = fn = 0
    results: list[dict[str, Any]] = []
    t0 = time.time()

    for clip in clips:
        ev = evaluate_video_clip(clip.video_path, engine, sensitivity=sensitivity)
        gt_fall = clip.is_fall_ground_truth
        pred_fall = ev["fall_detected"]
        if gt_fall and pred_fall:
            tp += 1
        elif not gt_fall and pred_fall:
            fp += 1
        elif not gt_fall and not pred_fall:
            tn += 1
        else:
            fn += 1

        results.append(
            {
                "subject": clip.subject,
                "category": clip.category,
                "file": clip.filename,
                "ground_truth_fall": gt_fall,
                "predicted_fall": pred_fall,
                "correct": gt_fall == pred_fall,
                "description": clip.description,
                "classes": clip.classes_raw,
                **ev,
            }
        )

    total = len(results)
    accuracy = round((tp + tn) / total * 100.0, 1) if total else 0.0
    precision = round(tp / (tp + fp) * 100.0, 1) if (tp + fp) else 0.0
    recall = round(tp / (tp + fn) * 100.0, 1) if (tp + fn) else 0.0

    return {
        "dataset": "GMDCSA-24",
        "doi": GMDCSA24_DOI,
        "sensitivity": sensitivity,
        "total_clips": total,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "accuracy_pct": accuracy,
        "precision_pct": precision,
        "recall_pct": recall,
        "elapsed_sec": round(time.time() - t0, 2),
        "clips": results,
    }
