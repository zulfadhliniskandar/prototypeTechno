"""Real-time AI Fall Detection using YOLOv11-Pose & a 4-State Machine.

State Machine:
    STANDING -> FALLING -> LYING -> FALL DETECTED
    If the person returns to an upright posture during FALLING (or before LYING
    confirmation completes), the event is cancelled and resets cleanly to STANDING.

Pose Analysis:
    Uses COCO 17 human body keypoints — especially shoulders (5, 6), hips (11, 12),
    knees (13, 14), and ankles (15, 16) — to estimate torso inclination angle,
    full-body axis orientation, leg orientation, and vertical descent velocity.
    Falls back to person bounding-box geometry when keypoints are occluded/unavailable.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
from ultralytics import YOLO

from .config import INFERENCE_IMGSZ, MODEL_PATH, SENSITIVITY_PRESETS

# Explicit State Machine constants
STATE_MONITORING = "MONITORING"
STATE_STANDING = "STANDING"
STATE_FALLING = "FALLING"
STATE_LYING = "LYING"
STATE_FALL_DETECTED = "FALL DETECTED"

# COCO 17-keypoint skeleton pairs
SKELETON_EDGES = [
    (0, 1), (0, 2), (1, 3), (2, 4),          # Head
    (5, 6), (5, 7), (7, 9), (6, 8), (8, 10), # Arms & shoulders
    (5, 11), (6, 12), (11, 12),              # Torso
    (11, 13), (13, 15), (12, 14), (14, 16),  # Legs
]

# Colors in BGR
COLOR_TEAL = (143, 143, 10)     # #0A8F8F
COLOR_GREEN = (96, 174, 39)     # #27AE60
COLOR_AMBER = (18, 156, 243)    # #F39C12
COLOR_ORANGE = (34, 126, 230)   # #E67E22
COLOR_RED = (60, 76, 231)       # #E74C3C
COLOR_WHITE = (255, 255, 255)


@dataclass
class PoseMetrics:
    """Extracted posture & kinematic measurements for a detected person."""

    box: tuple[int, int, int, int]
    box_conf: float
    aspect_ratio: float
    torso_angle: float
    body_angle: float
    leg_angle: float
    head_y_norm: float
    shoulder_y_norm: float
    hip_y_norm: float
    ankle_y_norm: float
    box_h_norm: float
    keypoint_source: str  # "full_pose" | "partial_pose" | "bbox_fallback"
    kpts: np.ndarray | None = None
    kconfs: np.ndarray | None = None


@dataclass
class DetectionResult:
    """Output of analyzing one video frame."""

    person_detected: bool = False
    posture: str = STATE_MONITORING   # MONITORING | STANDING | FALLING | LYING | FALL DETECTED
    fall_triggered: bool = False
    fall_cancelled: bool = False
    inactivity_triggered: bool = False
    motion_triggered: bool = False
    confidence: float = 0.0
    torso_angle: float = 0.0
    body_angle: float = 0.0
    aspect_ratio: float = 0.0
    keypoint_source: str = "none"
    motion_score: float = 0.0
    cancelled_count: int = 0
    annotated_frame: np.ndarray | None = None
    details: dict[str, Any] = field(default_factory=dict)


class PoseEngine:
    """Thread-safe shared YOLOv11-Pose model wrapper."""

    def __init__(self, model_path: str | Any = MODEL_PATH) -> None:
        self._lock = threading.Lock()
        self.model_path = str(model_path)
        self._model = YOLO(self.model_path)
        dummy = np.zeros((320, 480, 3), dtype=np.uint8)
        with self._lock:
            self._model(dummy, imgsz=INFERENCE_IMGSZ, verbose=False)

    def infer(self, frame: np.ndarray, conf: float = 0.35):
        with self._lock:
            results = self._model(frame, imgsz=INFERENCE_IMGSZ, conf=conf, verbose=False)
        return results[0]


def _midpoint(
    kpts: np.ndarray, kconf: np.ndarray, i1: int, i2: int, min_c: float = 0.30
) -> tuple[np.ndarray, float] | None:
    c1, c2 = float(kconf[i1]), float(kconf[i2])
    if c1 >= min_c and c2 >= min_c:
        return ((kpts[i1] + kpts[i2]) * 0.5, (c1 + c2) * 0.5)
    if c1 >= min_c:
        return (kpts[i1], c1)
    if c2 >= min_c:
        return (kpts[i2], c2)
    return None


def _angle_from_vertical(p_top: np.ndarray, p_bottom: np.ndarray) -> float:
    """Angle in degrees [0..90] from vertical axis (0=upright, 90=horizontal)."""
    dx = float(p_top[0] - p_bottom[0])
    dy = float(p_top[1] - p_bottom[1])
    return math.degrees(math.atan2(abs(dx), max(abs(dy), 1e-5)))


def extract_pose_metrics(
    box: np.ndarray,
    box_conf: float,
    frame_w: int,
    frame_h: int,
    kpts: np.ndarray | None,
    kconfs: np.ndarray | None,
) -> PoseMetrics:
    """
    Estimate posture angles and vertical coordinates from shoulders, hips, knees, and ankles.
    Falls back to bounding box geometry whenever keypoints are temporarily unavailable.
    """
    x1, y1, x2, y2 = [float(v) for v in box]
    bw = max(x2 - x1, 1.0)
    bh = max(y2 - y1, 1.0)
    aspect_ratio = bw / bh

    # Bounding-box fallback defaults
    bbox_angle = min(90.0, max(0.0, math.degrees(math.atan2(bw, bh))))
    head_y_norm = y1 / frame_h
    shoulder_y_norm = (y1 + bh * 0.22) / frame_h
    hip_y_norm = (y1 + bh * 0.52) / frame_h
    ankle_y_norm = y2 / frame_h
    torso_angle = bbox_angle
    body_angle = bbox_angle
    leg_angle = bbox_angle
    keypoint_source = "bbox_fallback"

    if kpts is not None and kconfs is not None and len(kpts) >= 17:
        sh_mid = _midpoint(kpts, kconfs, 5, 6)    # Shoulders
        hp_mid = _midpoint(kpts, kconfs, 11, 12)  # Hips
        kn_mid = _midpoint(kpts, kconfs, 13, 14)  # Knees
        ak_mid = _midpoint(kpts, kconfs, 15, 16)  # Ankles
        head_pts = [kpts[i] for i in (0, 1, 2, 3, 4) if kconfs[i] >= 0.30]

        if head_pts:
            head_y_norm = float(np.mean([p[1] for p in head_pts])) / frame_h
        if sh_mid is not None:
            shoulder_y_norm = float(sh_mid[0][1]) / frame_h
        if hp_mid is not None:
            hip_y_norm = float(hp_mid[0][1]) / frame_h
        if ak_mid is not None:
            ankle_y_norm = float(ak_mid[0][1]) / frame_h
        elif kn_mid is not None:
            ankle_y_norm = float(kn_mid[0][1]) / frame_h

        # Estimate torso angle (shoulders -> hips, or head -> hips/knees)
        upper_pt = sh_mid[0] if sh_mid is not None else (
            np.mean(head_pts, axis=0) if head_pts else None
        )
        lower_torso_pt = hp_mid[0] if hp_mid is not None else (
            kn_mid[0] if kn_mid is not None else None
        )
        lower_body_pt = ak_mid[0] if ak_mid is not None else (
            kn_mid[0] if kn_mid is not None else hp_mid[0] if hp_mid is not None else None
        )

        if sh_mid is not None and hp_mid is not None:
            torso_angle = _angle_from_vertical(sh_mid[0], hp_mid[0])
            if ak_mid is not None or kn_mid is not None:
                keypoint_source = "full_pose"
                body_angle = _angle_from_vertical(sh_mid[0], lower_body_pt)
                leg_angle = _angle_from_vertical(hp_mid[0], lower_body_pt)
            else:
                keypoint_source = "partial_pose"
                body_angle = torso_angle
        elif upper_pt is not None and lower_torso_pt is not None:
            keypoint_source = "partial_pose"
            torso_angle = _angle_from_vertical(upper_pt, lower_torso_pt)
            body_angle = (
                _angle_from_vertical(upper_pt, lower_body_pt)
                if lower_body_pt is not None
                else torso_angle
            )
            if hp_mid is not None and lower_body_pt is not None:
                leg_angle = _angle_from_vertical(hp_mid[0], lower_body_pt)
        elif hp_mid is not None and (kn_mid is not None or ak_mid is not None):
            keypoint_source = "partial_pose"
            leg_angle = _angle_from_vertical(hp_mid[0], lower_body_pt)
            torso_angle = max(bbox_angle, leg_angle * 0.7)
            body_angle = max(bbox_angle, leg_angle)

    return PoseMetrics(
        box=(int(x1), int(y1), int(x2), int(y2)),
        box_conf=float(box_conf),
        aspect_ratio=aspect_ratio,
        torso_angle=torso_angle,
        body_angle=body_angle,
        leg_angle=leg_angle,
        head_y_norm=head_y_norm,
        shoulder_y_norm=shoulder_y_norm,
        hip_y_norm=hip_y_norm,
        ankle_y_norm=ankle_y_norm,
        box_h_norm=bh / frame_h,
        keypoint_source=keypoint_source,
        kpts=kpts,
        kconfs=kconfs,
    )


class CameraFallAnalyzer:
    """Stateful per-camera 4-state fall machine (STANDING -> FALLING -> LYING -> FALL DETECTED)."""

    def __init__(self, pose_engine: PoseEngine | None = None) -> None:
        self.pose_engine = pose_engine
        self._prev_gray: np.ndarray | None = None
        self._bg_sub = cv2.createBackgroundSubtractorMOG2(
            history=180, varThreshold=40, detectShadows=False
        )
        # History entries: (ts, head_y, shoulder_y, hip_y, box_h_norm, torso_angle)
        self._history: deque[tuple[float, float, float, float, float, float]] = deque(maxlen=60)

        # Explicit state machine fields
        self.state: str = STATE_MONITORING
        self._state_entered_ts: float = 0.0
        self._falling_started_ts: float | None = None
        self._lying_started_ts: float | None = None
        self._upright_streak: int = 0
        self._missing_streak: int = 0
        self._rapid_drop_seen: bool = False
        self._last_fall_alert_ts: float = -1e9
        self._last_motion_event_ts: float = 0.0
        self._last_motion_seen_ts: float = time.time()
        self._last_inactivity_alert_ts: float = 0.0
        self._contour_horiz_start_ts: float | None = None
        self.cancelled_count: int = 0

    def reset_state(self, now: float | None = None) -> None:
        self.state = STATE_MONITORING
        self._state_entered_ts = now or time.time()
        self._falling_started_ts = None
        self._lying_started_ts = None
        self._upright_streak = 0
        self._rapid_drop_seen = False

    @staticmethod
    def _in_roi(cx: float, cy: float, w: int, h: int, roi: dict[str, float]) -> bool:
        rx = float(roi.get("x", 0.0)) * w
        ry = float(roi.get("y", 0.0)) * h
        rw = float(roi.get("w", 1.0)) * w
        rh = float(roi.get("h", 1.0)) * h
        return rx <= cx <= (rx + rw) and ry <= cy <= (ry + rh)

    def evaluate_pose_state(
        self,
        metrics: PoseMetrics,
        now: float,
        camera_cfg: dict[str, Any],
        cooldown_seconds: float = 15.0,
    ) -> tuple[str, bool, bool, float, dict[str, Any]]:
        """
        Advance the explicit 4-state machine using pose metrics (or bounding-box fallback).
        Returns: (state, fall_triggered, fall_cancelled, confidence_pct, details)
        """
        sensitivity = camera_cfg.get("sensitivity", "medium")
        preset = SENSITIVITY_PRESETS.get(sensitivity, SENSITIVITY_PRESETS["medium"])

        self._history.append(
            (
                now,
                metrics.head_y_norm,
                metrics.shoulder_y_norm,
                metrics.hip_y_norm,
                metrics.box_h_norm,
                metrics.torso_angle,
            )
        )

        # Compute vertical descent delta and speed over recent 1.8s window
        recent = [item for item in self._history if 0.0 < (now - item[0]) <= 1.8]
        drop_delta = 0.0
        hip_drop = 0.0
        height_collapse = 0.0
        hip_vy = 0.0
        if recent:
            min_head = min(item[1] for item in recent)
            min_hip = min(item[3] for item in recent)
            max_h = max(item[4] for item in recent)
            drop_delta = metrics.head_y_norm - min_head
            hip_drop = metrics.hip_y_norm - min_hip
            height_collapse = max_h - metrics.box_h_norm

            # Instantaneous descent rate over ~0.3-0.7s
            short_prev = [item for item in recent if 0.15 <= (now - item[0]) <= 0.7]
            if short_prev:
                ref = short_prev[-1]
                dt = max(now - ref[0], 0.05)
                hip_vy = max((metrics.hip_y_norm - ref[3]) / dt, (metrics.shoulder_y_norm - ref[2]) / dt)

        rapid_drop = (
            drop_delta >= preset["drop_norm_thresh"]
            or hip_drop >= preset["drop_norm_thresh"] * 0.85
            or hip_vy >= preset["drop_speed_thresh"]
            or (metrics.keypoint_source == "bbox_fallback" and height_collapse >= preset["drop_norm_thresh"])
        )

        ang = metrics.torso_angle
        body_ang = metrics.body_angle
        ar = metrics.aspect_ratio

        # Posture classifiers using shoulders, hips, knees, ankles + bbox fallback
        is_upright = (
            ang <= preset["upright_max_deg"]
            and body_ang <= preset["upright_max_deg"] + 8.0
            and ar < 0.90
            and not rapid_drop
        )

        if metrics.keypoint_source == "bbox_fallback":
            is_lying = ar >= preset["aspect_ratio"] or ang >= preset["lying_torso_angle_deg"]
            is_falling_pose = (ar >= 0.82 or ang >= preset["falling_angle_deg"]) and not is_upright
        else:
            # Horizontal torso OR full-body horizontal alignment + low/collapsed posture
            is_lying = (
                ang >= preset["lying_torso_angle_deg"]
                or (body_ang >= preset["lying_body_angle_deg"] and ar >= 0.95)
                or (ar >= preset["aspect_ratio"] and ang >= 38.0)
                or (ar >= preset["aspect_ratio"] + 0.25)
            )
            is_falling_pose = (
                is_lying
                or ang >= preset["falling_angle_deg"]
                or body_ang >= preset["falling_angle_deg"] + 6.0
                or rapid_drop
            )

        # Confidence estimation
        angle_score = min(1.0, max(0.0, (max(ang, body_ang * 0.9) - 28.0) / 52.0))
        ar_score = min(1.0, max(0.0, (ar - 0.75) / 1.0))
        drop_bonus = 0.14 if (rapid_drop or self._rapid_drop_seen) else 0.0
        pose_bonus = 0.05 if metrics.keypoint_source == "full_pose" else 0.0
        conf_pct = round(
            min(
                98.0,
                max(
                    66.0,
                    (0.45 * angle_score + 0.30 * ar_score + 0.18 * metrics.box_conf + drop_bonus + pose_bonus)
                    * 100.0,
                ),
            ),
            1,
        )

        fall_triggered = False
        fall_cancelled = False
        details: dict[str, Any] = {
            "state": self.state,
            "torso_angle": round(ang, 1),
            "body_angle": round(body_ang, 1),
            "leg_angle": round(metrics.leg_angle, 1),
            "aspect_ratio": round(ar, 2),
            "keypoint_source": metrics.keypoint_source,
            "rapid_drop": rapid_drop or self._rapid_drop_seen,
            "drop_delta": round(drop_delta, 3),
            "hip_vy": round(hip_vy, 3),
        }

        if self.state in (STATE_MONITORING, STATE_STANDING):
            self.state = STATE_STANDING
            if is_lying or is_falling_pose:
                # Enter FALLING state first (explicit state flow: STANDING -> FALLING -> LYING -> FALL DETECTED)
                self.state = STATE_FALLING
                self._state_entered_ts = now
                self._falling_started_ts = now
                self._lying_started_ts = None
                self._upright_streak = 0
                self._rapid_drop_seen = rapid_drop
            else:
                conf_pct = round(metrics.box_conf * 100.0, 1)

        elif self.state == STATE_FALLING:
            if rapid_drop:
                self._rapid_drop_seen = True

            if is_upright:
                self._upright_streak += 1
                # Cancel event if person returns upright during FALLING stage
                if self._upright_streak >= 2 or (now - self._falling_started_ts >= 0.25 and ang <= 22.0):
                    self.state = STATE_STANDING
                    self._state_entered_ts = now
                    self._falling_started_ts = None
                    self._lying_started_ts = None
                    self._rapid_drop_seen = False
                    self._upright_streak = 0
                    self.cancelled_count += 1
                    fall_cancelled = True
                    conf_pct = round(metrics.box_conf * 100.0, 1)
            elif is_lying:
                self._upright_streak = 0
                self.state = STATE_LYING
                self._state_entered_ts = now
                self._lying_started_ts = now
            else:
                self._upright_streak = 0
                # Timeout stale FALLING if neither lying nor upright for > 3.5s
                if self._falling_started_ts and (now - self._falling_started_ts > 3.5):
                    self.state = STATE_STANDING
                    self._falling_started_ts = None
                    self._rapid_drop_seen = False
                    self.cancelled_count += 1
                    fall_cancelled = True

        elif self.state == STATE_LYING:
            if rapid_drop:
                self._rapid_drop_seen = True
            if is_upright:
                self._upright_streak += 1
                if self._upright_streak >= 2:
                    self.state = STATE_STANDING
                    self._state_entered_ts = now
                    self._falling_started_ts = None
                    self._lying_started_ts = None
                    self._rapid_drop_seen = False
                    self._upright_streak = 0
                    self.cancelled_count += 1
                    fall_cancelled = True
            elif is_lying:
                self._upright_streak = 0
                lying_elapsed = now - (self._lying_started_ts or now)
                # Require longer confirmation when no rapid vertical drop preceded LYING (e.g. lying in bed)
                req_confirm = (
                    preset["confirm_seconds"] * 0.65
                    if self._rapid_drop_seen
                    else preset["confirm_seconds"] * preset.get("slow_confirm_multiplier", 4.5)
                )
                if lying_elapsed >= req_confirm:
                    self.state = STATE_FALL_DETECTED
                    self._state_entered_ts = now
                    if now - self._last_fall_alert_ts >= cooldown_seconds:
                        fall_triggered = True
                        self._last_fall_alert_ts = now
                        details["lying_confirm_sec"] = round(lying_elapsed, 2)
            else:
                # Transitional posture between falling and lying
                self._upright_streak = 0

        elif self.state == STATE_FALL_DETECTED:
            if is_upright:
                self._upright_streak += 1
                if self._upright_streak >= 2:
                    self.state = STATE_STANDING
                    self._state_entered_ts = now
                    self._falling_started_ts = None
                    self._lying_started_ts = None
                    self._rapid_drop_seen = False
                    self._upright_streak = 0
            elif is_lying:
                self._upright_streak = 0
                if now - self._last_fall_alert_ts >= cooldown_seconds:
                    fall_triggered = True
                    self._last_fall_alert_ts = now
            else:
                self._upright_streak = 0

        details["state"] = self.state
        details["cancelled_count"] = self.cancelled_count
        return self.state, fall_triggered, fall_cancelled, conf_pct, details

    def analyze(
        self,
        frame: np.ndarray,
        camera_cfg: dict[str, Any],
        cooldown_seconds: float = 15.0,
        draw_skeleton: bool = True,
        frame_ts: float | None = None,
    ) -> DetectionResult:
        now = frame_ts if frame_ts is not None else time.time()
        h, w = frame.shape[:2]
        annotated = frame.copy()
        roi = camera_cfg.get("roi") or {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
        sensitivity = camera_cfg.get("sensitivity", "medium")
        preset = SENSITIVITY_PRESETS.get(sensitivity, SENSITIVITY_PRESETS["medium"])

        # 1. Lightweight motion score inside ROI
        rx1 = max(int(float(roi.get("x", 0.0)) * w), 0)
        ry1 = max(int(float(roi.get("y", 0.0)) * h), 0)
        rx2 = min(int((float(roi.get("x", 0.0)) + float(roi.get("w", 1.0))) * w), w)
        ry2 = min(int((float(roi.get("y", 0.0)) + float(roi.get("h", 1.0))) * h), h)
        if rx2 - rx1 < 20 or ry2 - ry1 < 20:
            rx1, ry1, rx2, ry2 = 0, 0, w, h

        roi_crop = frame[ry1:ry2, rx1:rx2]
        gray = cv2.cvtColor(roi_crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (9, 9), 0)
        motion_score = 0.0
        if self._prev_gray is not None and self._prev_gray.shape == gray.shape:
            diff = cv2.absdiff(self._prev_gray, gray)
            _, thresh = cv2.threshold(diff, 22, 255, cv2.THRESH_BINARY)
            motion_score = float(np.count_nonzero(thresh)) / max(thresh.size, 1) * 100.0
        self._prev_gray = gray

        if motion_score > 1.2:
            self._last_motion_seen_ts = now

        res = DetectionResult(
            posture=self.state,
            motion_score=round(motion_score, 2),
            cancelled_count=self.cancelled_count,
        )

        # Draw ROI outline if cropped smaller than full frame
        if rx1 > 4 or ry1 > 4 or rx2 < w - 4 or ry2 < h - 4:
            cv2.rectangle(annotated, (rx1, ry1), (rx2, ry2), COLOR_TEAL, 2)
            cv2.putText(
                annotated,
                "Detection Zone",
                (rx1 + 6, max(ry1 + 20, 20)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                COLOR_TEAL,
                1,
                cv2.LINE_AA,
            )

        # 2. YOLOv11-Pose inference
        fall_enabled = bool(camera_cfg.get("fall_detection_enabled", True))
        pose_res = (
            self.pose_engine.infer(frame, conf=0.35)
            if (fall_enabled and self.pose_engine is not None)
            else None
        )

        best_metrics: PoseMetrics | None = None
        if pose_res is not None and pose_res.boxes is not None and len(pose_res.boxes) > 0:
            boxes = pose_res.boxes.xyxy.cpu().numpy()
            bconfs = pose_res.boxes.conf.cpu().numpy()
            kpts_all = (
                pose_res.keypoints.xy.cpu().numpy()
                if pose_res.keypoints is not None
                else None
            )
            kconfs_all = (
                pose_res.keypoints.conf.cpu().numpy()
                if pose_res.keypoints is not None and pose_res.keypoints.conf is not None
                else None
            )

            for idx, box in enumerate(boxes):
                x1, y1, x2, y2 = box
                cx, cy = (x1 + x2) * 0.5, (y1 + y2) * 0.5
                if not self._in_roi(cx, cy, w, h, roi):
                    continue
                kpts = kpts_all[idx] if kpts_all is not None else None
                kconfs = kconfs_all[idx] if kconfs_all is not None else None
                cand = extract_pose_metrics(box, float(bconfs[idx]), w, h, kpts, kconfs)
                if best_metrics is None or cand.torso_angle > best_metrics.torso_angle:
                    best_metrics = cand

        # 3. Advance State Machine if person detected
        if best_metrics is not None:
            self._missing_streak = 0
            res.person_detected = True
            self._last_motion_seen_ts = now
            res.aspect_ratio = round(best_metrics.aspect_ratio, 2)
            res.torso_angle = round(best_metrics.torso_angle, 1)
            res.body_angle = round(best_metrics.body_angle, 1)
            res.keypoint_source = best_metrics.keypoint_source

            state, fall_trig, fall_canc, conf_pct, details = self.evaluate_pose_state(
                best_metrics, now, camera_cfg, cooldown_seconds=cooldown_seconds
            )
            res.posture = state
            res.fall_triggered = fall_trig
            res.fall_cancelled = fall_canc
            res.confidence = conf_pct
            res.cancelled_count = self.cancelled_count
            res.details = details

            box_color = (
                COLOR_RED
                if state == STATE_FALL_DETECTED
                else (
                    COLOR_ORANGE
                    if state == STATE_LYING
                    else (COLOR_AMBER if state == STATE_FALLING else COLOR_GREEN)
                )
            )

            bx1, by1, bx2, by2 = best_metrics.box
            if draw_skeleton:
                cv2.rectangle(annotated, (bx1, by1), (bx2, by2), box_color, 2)
                src_tag = "BBOX" if best_metrics.keypoint_source == "bbox_fallback" else "POSE"
                label = (
                    f"{state} {res.confidence:.0f}%"
                    if state in (STATE_LYING, STATE_FALL_DETECTED)
                    else f"{state} | {best_metrics.torso_angle:.0f}deg [{src_tag}]"
                )
                cv2.rectangle(
                    annotated, (bx1, max(by1 - 24, 0)), (min(w, bx1 + 255), by1), box_color, -1
                )
                cv2.putText(
                    annotated,
                    label,
                    (bx1 + 6, max(by1 - 7, 15)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.48,
                    COLOR_WHITE,
                    1,
                    cv2.LINE_AA,
                )
                kpts = best_metrics.kpts
                kconfs = best_metrics.kconfs
                if kpts is not None and kconfs is not None:
                    for i1, i2 in SKELETON_EDGES:
                        if kconfs[i1] >= 0.30 and kconfs[i2] >= 0.30:
                            p1 = (int(kpts[i1][0]), int(kpts[i1][1]))
                            p2 = (int(kpts[i2][0]), int(kpts[i2][1]))
                            cv2.line(annotated, p1, p2, box_color, 2, cv2.LINE_AA)
                    for kp, kc in zip(kpts, kconfs):
                        if kc >= 0.30:
                            cv2.circle(
                                annotated,
                                (int(kp[0]), int(kp[1])),
                                4,
                                COLOR_WHITE,
                                -1,
                                cv2.LINE_AA,
                            )
        else:
            self._missing_streak += 1
            if self._missing_streak > 6 and self.state != STATE_MONITORING:
                self.reset_state(now)
            res.posture = self.state

            # Optional motion-contour assist using same state-machine stages (for synthetic/far feeds)
            if fall_enabled and bool(camera_cfg.get("motion_assist_enabled", False)):
                lr = 0.0 if self._contour_horiz_start_ts is not None else 0.08
                fg = self._bg_sub.apply(roi_crop, learningRate=lr)
                fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
                fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
                contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                min_area = roi_crop.shape[0] * roi_crop.shape[1] * 0.015
                horiz_candidates = []
                for c in contours:
                    if cv2.contourArea(c) <= min_area:
                        continue
                    cx, cy, cw, ch = cv2.boundingRect(c)
                    car = cw / max(ch, 1)
                    if car >= preset["aspect_ratio"] and (cy + ch) > roi_crop.shape[0] * 0.50:
                        horiz_candidates.append((cv2.contourArea(c), cx, cy, cw, ch, car))

                # Inspect floor zone (bottom 28%) so upper standing ghost cannot mask horizontal floor body
                if not horiz_candidates:
                    floor_y0 = int(roi_crop.shape[0] * 0.72)
                    fg_floor = fg[floor_y0:, :]
                    f_contours, _ = cv2.findContours(
                        fg_floor, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                    )
                    for c in f_contours:
                        if cv2.contourArea(c) <= min_area * 0.85:
                            continue
                        cx, cy, cw, ch = cv2.boundingRect(c)
                        car = cw / max(ch, 1)
                        if car >= preset["aspect_ratio"] + 0.40 and cw >= roi_crop.shape[1] * 0.28:
                            horiz_candidates.append(
                                (cv2.contourArea(c), cx, floor_y0 + cy, cw, ch, car)
                            )

                if horiz_candidates:
                    _, cx, cy, cw, ch, car = max(horiz_candidates, key=lambda x: x[0])
                    res.aspect_ratio = round(car, 2)
                    res.keypoint_source = "bbox_fallback"
                    if self._contour_horiz_start_ts is None:
                        self._contour_horiz_start_ts = now
                    elapsed = now - self._contour_horiz_start_ts
                    if elapsed < preset["confirm_seconds"] * 0.4:
                        self.state = STATE_FALLING
                    elif elapsed < preset["confirm_seconds"]:
                        self.state = STATE_LYING
                    else:
                        self.state = STATE_FALL_DETECTED
                        res.person_detected = True
                        res.confidence = 84.0
                        if now - self._last_fall_alert_ts >= cooldown_seconds:
                            res.fall_triggered = True
                            self._last_fall_alert_ts = now
                    res.posture = self.state
                    if draw_skeleton:
                        col = COLOR_RED if self.state == STATE_FALL_DETECTED else COLOR_AMBER
                        cv2.rectangle(
                            annotated,
                            (rx1 + cx, ry1 + cy),
                            (rx1 + cx + cw, ry1 + cy + ch),
                            col,
                            2,
                        )
                else:
                    if self._contour_horiz_start_ts is not None and self.state in (
                        STATE_FALLING,
                        STATE_LYING,
                    ):
                        self.cancelled_count += 1
                        res.fall_cancelled = True
                        self.state = STATE_STANDING
                        res.posture = self.state
                    self._contour_horiz_start_ts = None

        # 4. Inactivity check
        if bool(camera_cfg.get("inactivity_enabled", True)):
            inactivity_sec = max(int(camera_cfg.get("inactivity_minutes", 30)), 1) * 60
            if (
                now - self._last_motion_seen_ts >= inactivity_sec
                and now - self._last_inactivity_alert_ts >= inactivity_sec
            ):
                res.inactivity_triggered = True
                res.confidence = 85.0
                self._last_inactivity_alert_ts = now

        # 5. Optional motion logging
        if bool(camera_cfg.get("motion_enabled", False)) and motion_score >= 4.0:
            if now - self._last_motion_event_ts >= 60.0:
                res.motion_triggered = True
                res.confidence = round(min(99.0, 55.0 + motion_score * 2.0), 1)
                self._last_motion_event_ts = now

        # 6. Draw clean HUD overlay bar
        hud_color = (
            COLOR_RED
            if res.posture == STATE_FALL_DETECTED
            else (
                COLOR_ORANGE
                if res.posture == STATE_LYING
                else (COLOR_AMBER if res.posture == STATE_FALLING else (180, 240, 235))
            )
        )
        cv2.rectangle(annotated, (0, 0), (w, 32), (24, 34, 44), -1)
        ts_str = time.strftime("%H:%M:%S")
        cam_label = f"{camera_cfg.get('room', 'Room')} | {camera_cfg.get('ip_address', '')}"
        cv2.putText(
            annotated,
            cam_label,
            (10, 21),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            COLOR_WHITE,
            1,
            cv2.LINE_AA,
        )
        cv2.putText(
            annotated,
            f"{res.posture}  {ts_str}",
            (max(w - 245, 180), 21),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            hud_color,
            1,
            cv2.LINE_AA,
        )

        res.annotated_frame = annotated
        return res
