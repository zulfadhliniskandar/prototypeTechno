"""Multi-phone camera orchestration, live streams, ring buffers, and real-time alert dispatch."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import cv2
import numpy as np
from aiohttp import web

from .config import (
    CAPTURES_DIR,
    POST_EVENT_RECORD_SECONDS,
    RING_BUFFER_SECONDS,
    TARGET_CAPTURE_FPS,
    get_lan_ips,
)
from .database import Database, utc_now_iso
from .fall_detector import CameraFallAnalyzer, PoseEngine
from .media_recorder import save_incident_photo, save_incident_video
from .network_discovery import build_candidate_urls, _extract_first_jpeg_from_bytes


@dataclass
class StandbyPhoneNode:
    """A smartphone browser connected on /phone-camera awaiting or actively streaming."""

    node_id: str
    ip_address: str
    device_label: str
    ws: web.WebSocketResponse
    camera_id: str | None = None
    battery_level: int | None = None
    connected_at: str = field(default_factory=utc_now_iso)


class CameraWorker:
    """Manages one connected phone camera's frame stream, AI analysis, ring buffer, and MJPEG feed."""

    def __init__(
        self,
        manager: CameraManager,
        camera_cfg: dict[str, Any],
    ) -> None:
        self.manager = manager
        self.cfg = camera_cfg
        self.camera_id: str = camera_cfg["id"]
        self.analyzer = CameraFallAnalyzer(manager.pose_engine)
        self.ring_buffer: deque[tuple[float, np.ndarray]] = deque(
            maxlen=RING_BUFFER_SECONDS * TARGET_CAPTURE_FPS
        )
        self.latest_annotated_jpeg: bytes | None = None
        self.latest_raw_frame: np.ndarray | None = None
        self.last_posture: str = "MONITORING"
        self.last_confidence: float = 0.0
        self.last_torso_angle: float = 0.0
        self.last_body_angle: float = 0.0
        self.last_keypoint_source: str = "none"
        self.fps_estimate: float = 0.0
        self._frame_times: deque[float] = deque(maxlen=20)
        self._task: asyncio.Task | None = None
        self._infer_task: asyncio.Task | None = None
        self._pending_frame: np.ndarray | None = None
        self._frame_ready = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._last_infer_ts: float = 0.0
        self._last_status_push_ts: float = 0.0
        self._recording_post_frames: list[tuple[float, np.ndarray]] | None = None
        self._post_record_until: float = 0.0
        self._active_event_id: str | None = None
        self._standby_ws: web.WebSocketResponse | None = None

    def start(self) -> None:
        self._stop_event.clear()
        if self._infer_task is None or self._infer_task.done():
            self._infer_task = asyncio.create_task(self._run_infer_loop())
        if self.cfg["source_type"] in ("ip_camera", "webcam", "simulator", "video_file"):
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run_pull_loop())
        elif self.cfg["source_type"] == "phone_node":
            # Phone pushes frames via WebSocket; mark connecting until frames arrive
            if not self._standby_ws:
                self._update_status("connecting")

    async def stop(self) -> None:
        self._stop_event.set()
        self._frame_ready.set()
        for t in (self._task, self._infer_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._infer_task = None

    def submit_frame(self, frame: np.ndarray) -> None:
        """Store latest received frame without blocking network readers (zero-lag drop-oldest)."""
        self._pending_frame = frame
        self._frame_ready.set()

    async def _run_infer_loop(self) -> None:
        while not self._stop_event.is_set():
            await self._frame_ready.wait()
            self._frame_ready.clear()
            if self._stop_event.is_set():
                break
            frame = self._pending_frame
            self._pending_frame = None
            if frame is not None:
                try:
                    await self.process_frame(frame)
                except Exception:
                    pass

    def update_config(self, new_cfg: dict[str, Any]) -> None:
        self.cfg = new_cfg
        if new_cfg.get("paused"):
            self._update_status("paused")

    def _update_status(self, status: str) -> None:
        if self.cfg.get("status") != status:
            self.cfg["status"] = status
            self.manager.db.update_camera(
                self.camera_id,
                {"status": status, "last_seen_at": utc_now_iso() if status == "online" else self.cfg.get("last_seen_at")},
            )
            asyncio.create_task(
                self.manager.broadcast_caregiver(
                    {
                        "type": "camera_status",
                        "camera": self.manager.get_camera_summary(self.camera_id),
                    }
                )
            )

    async def process_frame(self, frame: np.ndarray) -> None:
        """Process a newly received BGR frame from either pull stream or phone WebSocket."""
        now = time.time()
        self.latest_raw_frame = frame
        self._frame_times.append(now)
        if len(self._frame_times) >= 2:
            span = max(self._frame_times[-1] - self._frame_times[0], 0.1)
            self.fps_estimate = round((len(self._frame_times) - 1) / span, 1)

        if self.cfg.get("paused") or self.manager.is_master_paused():
            self._update_status("paused")
            # Encode dimmed paused frame
            overlay = frame.copy()
            overlay = (overlay * 0.45).astype(np.uint8)
            cv2.putText(
                overlay,
                "PRIVACY PAUSED",
                (20, 45),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.9,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
            ok, jpg = cv2.imencode(".jpg", overlay, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                self.latest_annotated_jpeg = jpg.tobytes()
            return

        if self.cfg.get("status") != "online":
            self._update_status("online")

        # Update last_seen periodically
        if now - self._last_status_push_ts > 10.0:
            self._last_status_push_ts = now
            self.manager.db.update_camera(self.camera_id, {"last_seen_at": utc_now_iso()})

        # Run AI inference at up to ~6 FPS per camera to keep CPU smooth across multiple phones
        settings = self.manager.db.get_settings()
        cooldown = float(settings.get("cooldown_seconds", 15))
        draw_skel = bool(settings.get("show_skeleton_overlay", True))

        if now - self._last_infer_ts >= 0.14:
            self._last_infer_ts = now
            prev_posture = self.last_posture
            det = await asyncio.to_thread(
                self.analyzer.analyze, frame, self.cfg, cooldown, draw_skel
            )
            annotated = det.annotated_frame if det.annotated_frame is not None else frame
            self.last_posture = det.posture
            self.last_confidence = det.confidence
            self.last_torso_angle = det.torso_angle
            self.last_body_angle = det.body_angle
            self.last_keypoint_source = det.keypoint_source

            if det.posture != prev_posture:
                asyncio.create_task(
                    self.manager.broadcast_caregiver(
                        {
                            "type": "camera_status",
                            "camera": self.manager.get_camera_summary(self.camera_id),
                        }
                    )
                )

            # Send posture feedback back to phone camera node if attached
            if self._standby_ws and not self._standby_ws.closed:
                try:
                    await self._standby_ws.send_json(
                        {
                            "type": "ai_feedback",
                            "posture": det.posture,
                            "confidence": det.confidence,
                            "person_detected": det.person_detected,
                        }
                    )
                except Exception:
                    pass

            if det.fall_triggered:
                await self._trigger_event(
                    "fall", det.confidence, annotated, det.details
                )
            elif det.inactivity_triggered:
                await self._trigger_event(
                    "inactivity", det.confidence, annotated, {"reason": "prolonged_inactivity"}
                )
            elif det.motion_triggered:
                await self._trigger_event(
                    "motion", det.confidence, annotated, {"motion_score": det.motion_score}
                )
        else:
            annotated = frame

        self.ring_buffer.append((now, annotated.copy()))

        # Collect post-event frames for video clip if recording
        if self._recording_post_frames is not None:
            self._recording_post_frames.append((now, annotated.copy()))
            if now >= self._post_record_until and self._active_event_id:
                frames_to_save = list(self._recording_post_frames)
                evt_id = self._active_event_id
                self._recording_post_frames = None
                self._active_event_id = None
                asyncio.create_task(self._finalize_video_clip(evt_id, frames_to_save))

        ok, jpg = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            self.latest_annotated_jpeg = jpg.tobytes()

    async def trigger_manual_test(self) -> dict[str, Any]:
        """Run a controlled test fall alert using current frame (or synthetic preview frame)."""
        frame = self.latest_raw_frame
        if frame is None:
            frame = np.full((480, 640, 3), 235, dtype=np.uint8)
            cv2.rectangle(frame, (0, 330), (640, 480), (210, 200, 190), -1)
            cv2.putText(
                frame,
                f"TEST FALL ALERT - {self.cfg['room']}",
                (45, 220),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.85,
                (60, 76, 231),
                2,
                cv2.LINE_AA,
            )
        else:
            frame = frame.copy()
            cv2.putText(
                frame,
                "TEST FALL VERIFICATION",
                (20, 65),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (60, 76, 231),
                2,
                cv2.LINE_AA,
            )

        if not self.ring_buffer:
            now = time.time()
            for i in range(12):
                self.ring_buffer.append((now - (12 - i) * 0.15, frame.copy()))

        return await self._trigger_event("fall", 94.0, frame, {"test_mode": True})

    async def _trigger_event(
        self,
        event_type: str,
        confidence: float,
        snapshot_frame: np.ndarray,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        event_id = f"evt-{uuid.uuid4().hex[:10]}"
        photo_filename = await asyncio.to_thread(
            save_incident_photo, snapshot_frame, event_id, CAPTURES_DIR
        )
        prefix = "[TEST] " if details.get("test_mode") else ""
        event = self.manager.db.create_event(
            {
                "id": event_id,
                "camera_id": self.camera_id,
                "camera_name": self.cfg["name"],
                "room": self.cfg["room"],
                "phone_ip": self.cfg["ip_address"],
                "event_type": event_type,
                "confidence": confidence,
                "photo_filename": photo_filename,
                "status": "pending" if event_type in ("fall", "inactivity") else "resolved",
                "initial_log": (
                    f"{prefix}{event_type.replace('_', ' ').title()} detected on "
                    f"{self.cfg['name']} ({self.cfg['ip_address']}) — {confidence:.0f}% AI confidence"
                ),
            }
        )

        # Start post-event video clip capture if this is a fall or inactivity alert
        if event_type == "fall":
            pre_frames = list(self.ring_buffer)[-50:]
            if not pre_frames:
                pre_frames = [(time.time(), snapshot_frame.copy())]
            if details.get("test_mode"):
                # Finalize immediately from buffer for fast test response
                asyncio.create_task(self._finalize_video_clip(event_id, pre_frames))
            else:
                self._recording_post_frames = pre_frames
                self._active_event_id = event_id
                self._post_record_until = time.time() + POST_EVENT_RECORD_SECONDS

        # Notify caregiver dashboards immediately
        await self.manager.broadcast_caregiver(
            {
                "type": "new_alert",
                "event": event,
            }
        )
        return event

    async def _finalize_video_clip(
        self, event_id: str, frames: list[tuple[float, np.ndarray]]
    ) -> None:
        video_filename, duration = await asyncio.to_thread(
            save_incident_video, frames, event_id, CAPTURES_DIR
        )
        if video_filename:
            updated = self.manager.db.update_event(
                event_id,
                {
                    "video_filename": video_filename,
                    "duration_sec": duration,
                    "log_actor": "System Recorder",
                    "log_action": f"Incident video clip saved ({duration:.1f}s MP4)",
                },
            )
            if updated:
                await self.manager.broadcast_caregiver(
                    {"type": "event_updated", "event": updated}
                )

    async def _run_pull_loop(self) -> None:
        """Pull frames continuously from a phone IP camera (MJPEG / snapshot / RTSP / webcam)."""
        backoff = 2.0
        while not self._stop_event.is_set():
            if self.cfg.get("paused") or self.manager.is_master_paused():
                self._update_status("paused")
                await asyncio.sleep(0.5)
                continue

            self._update_status("connecting")
            source_type = self.cfg["source_type"]
            url = self.cfg.get("stream_url", "")

            if source_type in ("webcam", "video_file") or url.isdigit():
                await self._pull_opencv_loop(
                    int(url) if url.isdigit() else url,
                    loop_file=(source_type == "video_file"),
                )
            elif url.lower().startswith("rtsp://"):
                await self._pull_opencv_loop(url)
            else:
                # Re-normalize if user changed IP/port
                if not url:
                    _, _, cands = build_candidate_urls(
                        self.cfg["ip_address"], self.cfg.get("port", 8080)
                    )
                    url = cands[0][1]
                await self._pull_http_stream(url)

            if self._stop_event.is_set():
                break
            self._update_status("offline")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 1.5, 10.0)

    async def _pull_http_stream(self, url: str) -> None:
        username = self.cfg.get("username", "")
        password = self.cfg.get("password", "")
        auth = aiohttp.BasicAuth(username, password) if username else None
        timeout = aiohttp.ClientTimeout(total=None, connect=4.0, sock_read=6.0)
        is_snapshot = url.lower().endswith((".jpg", ".jpeg", ".png")) or "shot.jpg" in url.lower()

        try:
            async with aiohttp.ClientSession(timeout=timeout, auth=auth) as session:
                if is_snapshot:
                    while not self._stop_event.is_set() and not self.cfg.get("paused"):
                        async with session.get(url, ssl=False) as resp:
                            if resp.status >= 400:
                                return
                            data = await resp.read()
                            arr = np.frombuffer(data, dtype=np.uint8)
                            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                            if frame is not None:
                                await self.process_frame(frame)
                        await asyncio.sleep(1.0 / TARGET_CAPTURE_FPS)
                else:
                    async with session.get(url, ssl=False) as resp:
                        if resp.status >= 400:
                            return
                        ctype = resp.headers.get("Content-Type", "").lower()
                        if "image/jpeg" in ctype and "multipart" not in ctype:
                            data = await resp.read()
                            arr = np.frombuffer(data, dtype=np.uint8)
                            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                            if frame is not None:
                                await self.process_frame(frame)
                            return

                        buf = b""
                        async for chunk in resp.content.iter_chunked(32768):
                            if self._stop_event.is_set() or self.cfg.get("paused"):
                                return
                            buf += chunk
                            if len(buf) > 4_000_000:
                                buf = buf[-500_000:]
                            latest_jpg = None
                            while True:
                                jpg = _extract_first_jpeg_from_bytes(buf)
                                if not jpg:
                                    break
                                end_idx = buf.find(b"\xff\xd9", buf.find(b"\xff\xd8") + 2)
                                buf = buf[end_idx + 2 :]
                                latest_jpg = jpg
                            if latest_jpg is not None:
                                arr = np.frombuffer(latest_jpg, dtype=np.uint8)
                                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                                if frame is not None:
                                    self.submit_frame(frame)
        except Exception:
            return

    async def _pull_opencv_loop(self, src: str | int, loop_file: bool = False) -> None:
        url = src
        username = self.cfg.get("username", "")
        password = self.cfg.get("password", "")
        if isinstance(url, str) and username and "://" in url and "@" not in url:
            scheme, rest = url.split("://", 1)
            url = f"{scheme}://{username}:{password}@{rest}"

        cap = await asyncio.to_thread(cv2.VideoCapture, url)
        if not cap.isOpened():
            cap.release()
            return
        try:
            native_fps = cap.get(cv2.CAP_PROP_FPS) or TARGET_CAPTURE_FPS
            stride = max(int(round(native_fps / TARGET_CAPTURE_FPS)), 1) if loop_file else 1
            idx = 0
            while not self._stop_event.is_set() and not self.cfg.get("paused"):
                ok, frame = await asyncio.to_thread(cap.read)
                if not ok or frame is None:
                    if loop_file:
                        await asyncio.to_thread(cap.set, cv2.CAP_PROP_POS_FRAMES, 0)
                        self.analyzer.reset_state()
                        idx = 0
                        continue
                    break
                if idx % stride == 0:
                    if loop_file:
                        await self.process_frame(frame)
                    else:
                        self.submit_frame(frame)
                    await asyncio.sleep(1.0 / TARGET_CAPTURE_FPS)
                idx += 1
        finally:
            await asyncio.to_thread(cap.release)


class CameraManager:
    """Central registry for all connected phone cameras and Caregiver WebSocket clients."""

    def __init__(self, db: Database, pose_engine: PoseEngine) -> None:
        self.db = db
        self.pose_engine = pose_engine
        self.workers: dict[str, CameraWorker] = {}
        self.caregiver_sockets: set[web.WebSocketResponse] = set()
        self.standby_nodes: dict[str, StandbyPhoneNode] = {}
        self._bg_tasks: set[asyncio.Task] = set()

    def is_master_paused(self) -> bool:
        res = self.db.get_resident_profile()
        return bool(res.get("master_paused", False))

    async def startup(self) -> None:
        """Start workers for all saved phone cameras on server boot."""
        for cam in self.db.list_cameras():
            # Reset stale online state on restart
            if cam["source_type"] == "phone_node":
                self.db.update_camera(cam["id"], {"status": "offline"})
                cam["status"] = "offline"
            worker = CameraWorker(self, cam)
            self.workers[cam["id"]] = worker
            worker.start()

        # Run retention purge if auto_delete enabled
        settings = self.db.get_settings()
        if settings.get("auto_delete", True):
            days = int(settings.get("retention_days", 30))
            self.db.purge_old_events(days, CAPTURES_DIR)

    async def shutdown(self) -> None:
        for w in list(self.workers.values()):
            await w.stop()
        for ws in list(self.caregiver_sockets):
            await ws.close()
        for node in list(self.standby_nodes.values()):
            await node.ws.close()

    def get_camera_summary(self, camera_id: str) -> dict[str, Any] | None:
        cam = self.db.get_camera(camera_id)
        if not cam:
            return None
        w = self.workers.get(camera_id)
        cam["live_posture"] = w.last_posture if w else "MONITORING"
        cam["live_confidence"] = w.last_confidence if w else 0.0
        cam["live_torso_angle"] = w.last_torso_angle if w else 0.0
        cam["live_body_angle"] = w.last_body_angle if w else 0.0
        cam["live_keypoint_source"] = w.last_keypoint_source if w else "none"
        cam["cancelled_count"] = w.analyzer.cancelled_count if w else 0
        cam["fps"] = w.fps_estimate if w else 0.0
        cam["has_frame"] = bool(w and w.latest_annotated_jpeg is not None)
        return cam

    def list_cameras_enriched(self) -> list[dict[str, Any]]:
        return [
            self.get_camera_summary(c["id"])  # type: ignore[misc]
            for c in self.db.list_cameras()
        ]

    def list_standby_nodes(self) -> list[dict[str, Any]]:
        out = []
        for node in self.standby_nodes.values():
            if not node.ws.closed:
                out.append(
                    {
                        "node_id": node.node_id,
                        "ip_address": node.ip_address,
                        "device_label": node.device_label,
                        "camera_id": node.camera_id,
                        "battery_level": node.battery_level,
                        "connected_at": node.connected_at,
                    }
                )
        return out

    async def add_camera(self, payload: dict[str, Any]) -> dict[str, Any]:
        cam = self.db.create_camera(payload)
        worker = CameraWorker(self, cam)
        self.workers[cam["id"]] = worker

        # If adding a phone_node by IP address, bind to matching unpaired standby node if waiting
        if cam["source_type"] == "phone_node":
            for node in self.standby_nodes.values():
                if node.ip_address == cam["ip_address"] and not node.camera_id and not node.ws.closed:
                    node.camera_id = cam["id"]
                    worker._standby_ws = node.ws
                    try:
                        await node.ws.send_json(
                            {
                                "type": "start_stream",
                                "camera_id": cam["id"],
                                "room": cam["room"],
                                "name": cam["name"],
                            }
                        )
                    except Exception:
                        pass
                    break

        worker.start()
        summary = self.get_camera_summary(cam["id"]) or cam
        await self.broadcast_caregiver({"type": "camera_added", "camera": summary})
        return summary

    async def update_camera(self, camera_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        updated = self.db.update_camera(camera_id, payload)
        if not updated:
            return None
        w = self.workers.get(camera_id)
        if w:
            full = self.db.get_camera(camera_id, include_secret=True)
            if full:
                w.update_config(full)
        summary = self.get_camera_summary(camera_id) or updated
        await self.broadcast_caregiver({"type": "camera_updated", "camera": summary})
        return summary

    async def remove_camera(self, camera_id: str) -> bool:
        w = self.workers.pop(camera_id, None)
        if w:
            if w._standby_ws and not w._standby_ws.closed:
                try:
                    await w._standby_ws.send_json({"type": "stop_stream"})
                except Exception:
                    pass
            await w.stop()
        for node in self.standby_nodes.values():
            if node.camera_id == camera_id:
                node.camera_id = None
        deleted = self.db.delete_camera(camera_id)
        if deleted:
            await self.broadcast_caregiver({"type": "camera_removed", "camera_id": camera_id})
        return deleted

    async def register_standby_node(self, node: StandbyPhoneNode) -> None:
        self.standby_nodes[node.node_id] = node
        # Only auto-reattach to an offline/connecting phone_node slot with no active WebSocket
        for cam in self.db.list_cameras():
            w = self.workers.get(cam["id"])
            has_active_ws = bool(w and w._standby_ws and not w._standby_ws.closed)
            if (
                cam["source_type"] == "phone_node"
                and cam["ip_address"] == node.ip_address
                and cam["status"] != "online"
                and not has_active_ws
            ):
                node.camera_id = cam["id"]
                if w:
                    w._standby_ws = node.ws
                await node.ws.send_json(
                    {
                        "type": "start_stream",
                        "camera_id": cam["id"],
                        "room": cam["room"],
                        "name": cam["name"],
                    }
                )
                break
        await self.broadcast_caregiver(
            {"type": "standby_nodes", "nodes": self.list_standby_nodes()}
        )

    async def unregister_standby_node(self, node_id: str) -> None:
        node = self.standby_nodes.pop(node_id, None)
        if node and node.camera_id:
            w = self.workers.get(node.camera_id)
            if w and w._standby_ws == node.ws:
                w._standby_ws = None
                w._update_status("offline")
        await self.broadcast_caregiver(
            {"type": "standby_nodes", "nodes": self.list_standby_nodes()}
        )

    async def activate_phone_node(
        self, node: StandbyPhoneNode, name: str, room: str
    ) -> dict[str, Any]:
        """Called when the phone taps 'Start Streaming' on /phone-camera."""
        existing = None
        if node.camera_id:
            existing = self.db.get_camera(node.camera_id)
        if not existing:
            # Reuse if same IP+name exists, otherwise allow multiple phones on same LAN or distinct IPs
            for cam in self.db.list_cameras():
                if (
                    cam["source_type"] == "phone_node"
                    and cam["ip_address"] == node.ip_address
                    and cam["room"].lower() == room.lower()
                ):
                    existing = cam
                    break

        if existing:
            cam = await self.update_camera(
                existing["id"],
                {
                    "name": name,
                    "room": room,
                    "status": "online",
                    "battery_level": node.battery_level,
                    "last_seen_at": utc_now_iso(),
                },
            )
            cam_id = existing["id"]
        else:
            cam = await self.add_camera(
                {
                    "name": name,
                    "room": room,
                    "source_type": "phone_node",
                    "ip_address": node.ip_address,
                    "port": 443,
                    "stream_url": f"wss://{get_lan_ips()[0]}:8443/ws/phone-node",
                    "status": "online",
                    "battery_level": node.battery_level,
                    "last_seen_at": utc_now_iso(),
                }
            )
            cam_id = cam["id"]

        node.camera_id = cam_id
        w = self.workers.get(cam_id)
        if w:
            w._standby_ws = node.ws
            w._update_status("online")

        await self.broadcast_caregiver(
            {"type": "standby_nodes", "nodes": self.list_standby_nodes()}
        )
        return cam  # type: ignore[return-value]

    async def broadcast_caregiver(self, message: dict[str, Any]) -> None:
        dead: list[web.WebSocketResponse] = []
        for ws in list(self.caregiver_sockets):
            if ws.closed:
                dead.append(ws)
                continue
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.caregiver_sockets.discard(ws)
