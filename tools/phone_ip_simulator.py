r"""Virtual Phone IP Camera Simulator (mimics Android IP Webcam on multiple IPs/ports).

Supports both synthetic figures AND replaying real GMDCSA-24 dataset videos over IP:
    python tools/phone_ip_simulator.py
    python tools/phone_ip_simulator.py --video "data/datasets/gmdcsa24/Subject 1/Fall/03.mp4"
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from aiohttp import web


class VideoFilePhoneFeed:
    """Loops a real video file (e.g. GMDCSA-24 clip) as an IP phone camera MJPEG feed."""

    def __init__(self, room_name: str, ip: str, port: int, video_path: Path) -> None:
        self.room_name = room_name
        self.ip = ip
        self.port = port
        self.video_path = Path(video_path)
        self._frames: list[bytes] = []
        self._fps: float = 12.0
        self._idx = 0
        self._load_video()

    def _load_video(self) -> None:
        cap = cv2.VideoCapture(str(self.video_path))
        if not cap.isOpened():
            raise RuntimeError(f"Failed to open video file: {self.video_path}")
        native_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        stride = max(int(round(native_fps / 12.0)), 1)
        self._fps = min(12.0, native_fps / stride)
        i = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                if i % stride == 0:
                    # Resize large 1080p clips cleanly to 640x360/480 for fast MJPEG streaming
                    h, w = frame.shape[:2]
                    if w > 854:
                        scale = 854.0 / w
                        frame = cv2.resize(frame, (854, int(h * scale)))
                    ok_enc, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
                    if ok_enc:
                        self._frames.append(jpg.tobytes())
                i += 1
        finally:
            cap.release()
        if not self._frames:
            raise RuntimeError(f"No frames extracted from {self.video_path}")

    def render_jpeg(self) -> bytes:
        jpg = self._frames[self._idx % len(self._frames)]
        self._idx += 1
        return jpg


class SimulatedPhoneFeed:
    """Renders a room feed with a moving/standing or falling figure."""

    def __init__(self, room_name: str, ip: str, port: int, fall_cycle_sec: float = 0.0) -> None:
        self.room_name = room_name
        self.ip = ip
        self.port = port
        self.fall_cycle_sec = fall_cycle_sec
        self.force_fallen = False
        self.start_ts = time.time()

    def is_fallen_now(self) -> bool:
        if self.force_fallen:
            return True
        if self.fall_cycle_sec > 0:
            phase = (time.time() - self.start_ts) % self.fall_cycle_sec
            return phase > (self.fall_cycle_sec * 0.65)
        return False

    def render_jpeg(self) -> bytes:
        w, h = 640, 480
        img = np.full((h, w, 3), (225, 222, 215), dtype=np.uint8)
        cv2.rectangle(img, (0, 320), (w, h), (185, 170, 155), -1)
        cv2.line(img, (0, 320), (w, 320), (140, 125, 110), 3)

        t = time.time()
        fallen = self.is_fallen_now()

        if not fallen:
            cx = int(320 + 60 * np.sin(t * 1.1))
            cv2.circle(img, (cx, 130), 22, (70, 80, 95), -1)
            cv2.rectangle(img, (cx - 26, 156), (cx + 26, 268), (45, 75, 115), -1)
            cv2.rectangle(img, (cx - 22, 268), (cx - 4, 370), (40, 45, 55), -1)
            cv2.rectangle(img, (cx + 4, 268), (cx + 22, 370), (40, 45, 55), -1)
            state_lbl = "STANDING"
        else:
            cx = 310
            cy = 395
            cv2.circle(img, (cx - 102, cy - 4), 24, (70, 80, 95), -1)
            cv2.rectangle(img, (cx - 84, cy - 26), (cx + 42, cy + 22), (45, 75, 115), -1)
            cv2.rectangle(img, (cx + 36, cy - 22), (cx + 140, cy - 2), (40, 45, 55), -1)
            cv2.rectangle(img, (cx + 36, cy + 2), (cx + 140, cy + 22), (40, 45, 55), -1)
            state_lbl = "FALLEN ON FLOOR"

        cv2.putText(
            img,
            f"IP Webcam Sim | {self.room_name} | {self.ip}:{self.port} [{state_lbl}]",
            (14, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (30, 40, 50),
            2,
            cv2.LINE_AA,
        )
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return jpg.tobytes() if ok else b""


def create_phone_sim_app(feed: SimulatedPhoneFeed | VideoFilePhoneFeed) -> web.Application:
    app = web.Application()

    async def shot_handler(_request: web.Request) -> web.Response:
        return web.Response(body=feed.render_jpeg(), content_type="image/jpeg")

    async def video_handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-cache",
            },
        )
        await resp.prepare(request)
        sleep_sec = 1.0 / getattr(feed, "_fps", 8.3)
        try:
            while True:
                jpg = feed.render_jpeg()
                part = (
                    f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n"
                ).encode("ascii") + jpg + b"\r\n"
                await resp.write(part)
                await asyncio.sleep(sleep_sec)
        except (asyncio.CancelledError, ConnectionResetError, Exception):
            pass
        return resp

    async def toggle_fall_handler(request: web.Request) -> web.Response:
        if not hasattr(feed, "force_fallen"):
            return web.json_response({"error": "Video file feed does not toggle"}, status=400)
        body: dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            pass
        feed.force_fallen = bool(body.get("fallen", not feed.force_fallen))
        return web.json_response({"fallen": feed.force_fallen})

    app.router.add_get("/shot.jpg", shot_handler)
    app.router.add_get("/video", video_handler)
    app.router.add_get("/videofeed", video_handler)
    app.router.add_post("/sim/fall", toggle_fall_handler)
    return app


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run simulated IP Phone Cameras")
    parser.add_argument("--fall-cycle", type=float, default=0.0, help="Auto-fall cycle seconds (0 = manual)")
    parser.add_argument("--video", type=Path, default=None, help="Optional real video file (e.g. GMDCSA-24 .mp4) for Phone 1")
    args = parser.parse_args()

    feed1: SimulatedPhoneFeed | VideoFilePhoneFeed = (
        VideoFilePhoneFeed("Living Room Phone (Video)", "127.0.0.1", 8081, args.video)
        if args.video
        else SimulatedPhoneFeed("Living Room Phone", "127.0.0.1", 8081, fall_cycle_sec=args.fall_cycle)
    )
    feed2 = SimulatedPhoneFeed("Bedroom Phone", "127.0.0.2", 8082, fall_cycle_sec=args.fall_cycle)

    specs = [(feed1, "127.0.0.1", 8081), (feed2, "127.0.0.2", 8082)]
    runners = []
    print("\nSimulated Phone IP Cameras running:")
    for feed, ip, port in specs:
        sim_app = create_phone_sim_app(feed)
        runner = web.AppRunner(sim_app)
        await runner.setup()
        site = web.TCPSite(runner, ip, port)
        await site.start()
        runners.append(runner)
        print(f"  - {feed.room_name}: http://{ip}:{port}/video  (connect using IP {ip} port {port})")

    print("\nPress Ctrl+C to stop.\n")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for r in runners:
            await r.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
