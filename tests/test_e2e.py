"""Comprehensive End-to-End Test Suite for SafeWatch Multi-Phone System."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
import cv2
import numpy as np
from aiohttp import web

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app import create_app
from safewatch.config import CAPTURES_DIR
from safewatch.database import Database
from safewatch.fall_detector import (
    STATE_FALL_DETECTED,
    STATE_FALLING,
    STATE_LYING,
    STATE_STANDING,
    CameraFallAnalyzer,
    PoseEngine,
    extract_pose_metrics,
)
from safewatch.media_recorder import save_incident_photo, save_incident_video
from safewatch.qr import encode_qr_matrix
from tools.phone_ip_simulator import SimulatedPhoneFeed, create_phone_sim_app


def _make_pose_kpts(posture: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Generate synthetic 17-keypoint COCO skeleton + box for standing, falling, or lying."""
    kpts = np.zeros((17, 2), dtype=np.float32)
    confs = np.full(17, 0.90, dtype=np.float32)

    if posture == "standing":
        # Upright around x=320, y=100..420
        kpts[0] = [320, 110]
        kpts[5], kpts[6] = [295, 160], [345, 160]    # shoulders
        kpts[11], kpts[12] = [300, 265], [340, 265]  # hips
        kpts[13], kpts[14] = [300, 345], [340, 345]  # knees
        kpts[15], kpts[16] = [300, 420], [340, 420]  # ankles
        box = np.array([275, 95, 365, 430], dtype=np.float32)
    elif posture == "falling":
        # Tilted ~40 deg forward/sideways descending
        kpts[0] = [275, 195]
        kpts[5], kpts[6] = [275, 225], [310, 235]
        kpts[11], kpts[12] = [325, 295], [355, 305]
        kpts[13], kpts[14] = [330, 360], [360, 365]
        kpts[15], kpts[16] = [320, 420], [355, 420]
        box = np.array([250, 180, 385, 425], dtype=np.float32)
    else:  # lying horizontal on floor
        kpts[0] = [180, 385]
        kpts[5], kpts[6] = [225, 380], [225, 405]
        kpts[11], kpts[12] = [320, 385], [320, 408]
        kpts[13], kpts[14] = [395, 390], [395, 412]
        kpts[15], kpts[16] = [460, 392], [460, 415]
        box = np.array([165, 368, 475, 425], dtype=np.float32)
    return kpts, confs, box


def test_state_machine_and_pose_analysis() -> None:
    cfg = {"sensitivity": "medium", "fall_detection_enabled": True}

    # 1. Test full transition: STANDING -> FALLING -> LYING -> FALL DETECTED
    an = CameraFallAnalyzer(pose_engine=None)
    t = 100.0
    visited: list[str] = []
    triggered = False

    sequence = (
        [("standing", 0.2)] * 3
        + [("falling", 0.25)] * 2
        + [("lying", 0.35)] * 5
    )
    for pose_name, dt in sequence:
        t += dt
        kpts, confs, box = _make_pose_kpts(pose_name)
        m = extract_pose_metrics(box, 0.92, 640, 480, kpts, confs)
        assert m.keypoint_source == "full_pose"
        state, trig, _, _, _ = an.evaluate_pose_state(m, t, cfg, cooldown_seconds=15.0)
        if not visited or visited[-1] != state:
            visited.append(state)
        triggered = triggered or trig

    assert visited == [STATE_STANDING, STATE_FALLING, STATE_LYING, STATE_FALL_DETECTED], (
        f"Unexpected state sequence: {visited}"
    )
    assert triggered is True, "Expected fall_triggered=True after LYING confirmation"

    # 2. Test cancellation when returning upright during FALLING
    an_cancel = CameraFallAnalyzer(pose_engine=None)
    t = 200.0
    cancel_visited: list[str] = []
    cancelled_seen = False
    triggered_cancel = False

    cancel_seq = (
        [("standing", 0.2)] * 2
        + [("falling", 0.25)] * 2
        + [("standing", 0.25)] * 3
    )
    for pose_name, dt in cancel_seq:
        t += dt
        kpts, confs, box = _make_pose_kpts(pose_name)
        m = extract_pose_metrics(box, 0.90, 640, 480, kpts, confs)
        state, trig, canc, _, _ = an_cancel.evaluate_pose_state(m, t, cfg)
        if not cancel_visited or cancel_visited[-1] != state:
            cancel_visited.append(state)
        cancelled_seen = cancelled_seen or canc
        triggered_cancel = triggered_cancel or trig

    assert cancel_visited == [STATE_STANDING, STATE_FALLING, STATE_STANDING], (
        f"Expected cancellation back to STANDING, got: {cancel_visited}"
    )
    assert cancelled_seen is True and an_cancel.cancelled_count == 1
    assert triggered_cancel is False, "Cancelled falling event must NOT trigger fall alert"

    # 3. Test bounding-box fallback when keypoints are temporarily unavailable
    an_bbox = CameraFallAnalyzer(pose_engine=None)
    t = 300.0
    bbox_visited: list[str] = []
    bbox_trig = False
    for pose_name, dt in [("standing", 0.2)] * 2 + [("falling", 0.25)] * 2 + [("lying", 0.45)] * 4:
        t += dt
        _, _, box = _make_pose_kpts(pose_name)
        m = extract_pose_metrics(box, 0.88, 640, 480, None, None)
        assert m.keypoint_source == "bbox_fallback"
        state, trig, _, _, _ = an_bbox.evaluate_pose_state(m, t, cfg)
        if not bbox_visited or bbox_visited[-1] != state:
            bbox_visited.append(state)
        bbox_trig = bbox_trig or trig

    assert bbox_visited[-1] == STATE_FALL_DETECTED and bbox_trig is True
    print("[PASS] 4-State Machine (STANDING->FALLING->LYING->FALL DETECTED), cancellation & bbox fallback verified")


async def test_qr_encoder() -> None:
    url = "https://172.17.96.217:8443/phone-camera"
    mat = encode_qr_matrix(url)
    pad = np.pad(mat.astype(np.uint8), 4, constant_values=0)
    img = ((1 - pad) * 255).astype(np.uint8)
    img = cv2.resize(img, (400, 400), interpolation=cv2.INTER_NEAREST)
    decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
    assert decoded == url, f"QR mismatch: {decoded}"
    print("[PASS] QR code generator verified with OpenCV decoder")


def test_media_recorder() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        frames = []
        now = time.time()
        for i in range(12):
            img = np.full((360, 640, 3), (200, 210, 220), dtype=np.uint8)
            cv2.circle(img, (100 + i * 25, 180), 30, (10, 143, 143), -1)
            frames.append((now + i * 0.12, img))

        photo = save_incident_photo(frames[-1][1], "evt-media-test", tmp_dir)
        assert (tmp_dir / photo).exists(), "Photo file not created"

        vid, dur = save_incident_video(frames, "evt-media-test", tmp_dir)
        assert vid is not None and (tmp_dir / vid).exists(), "MP4 clip not created"
        assert (tmp_dir / vid).stat().st_size > 1000, "MP4 clip unexpectedly small"
        cap = cv2.VideoCapture(str(tmp_dir / vid))
        assert cap.isOpened(), "VideoCapture could not open saved MP4"
        ok, f = cap.read()
        cap.release()
        assert ok and f is not None, "Could not read frame from saved MP4"
        print(f"[PASS] Media recorder created JPG + playable MP4 ({dur}s)")


def _free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


async def test_full_stack_and_browser() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test_safewatch.db")
        engine = PoseEngine()
        p_sim1 = _free_port("127.0.0.1")
        p_sim2 = _free_port("127.0.0.2")
        p_app = _free_port("127.0.0.1")

        feed1 = SimulatedPhoneFeed("Living Room Phone", "127.0.0.1", p_sim1)
        feed2 = SimulatedPhoneFeed("Bedroom Phone", "127.0.0.2", p_sim2)
        sim1 = web.AppRunner(create_phone_sim_app(feed1))
        sim2 = web.AppRunner(create_phone_sim_app(feed2))
        app = create_app(db=db, pose_engine=engine, https_enabled=False, http_port=p_app)
        runner = web.AppRunner(app)

        base = f"http://127.0.0.1:{p_app}"
        ws_alerts: list[dict] = []

        try:
            await sim1.setup()
            await sim2.setup()
            await web.TCPSite(sim1, "127.0.0.1", p_sim1).start()
            await web.TCPSite(sim2, "127.0.0.2", p_sim2).start()
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", p_app).start()

            async with aiohttp.ClientSession() as session:
                # Subscribe Caregiver WebSocket
                caregiver_ws = await session.ws_connect(f"ws://127.0.0.1:{p_app}/ws/caregiver")

                async def _ws_reader():
                    async for msg in caregiver_ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            ws_alerts.append(json.loads(msg.data))

                ws_task = asyncio.create_task(_ws_reader())

                # Probe Phone 1 IP
                async with session.post(
                    f"{base}/api/cameras/probe",
                    json={"ip_address": "127.0.0.1", "port": p_sim1},
                ) as r:
                    assert r.status == 200
                    probe1 = await r.json()
                    assert probe1["reachable"] is True
                    assert probe1["width"] == 640

                # Connect Phone 1 by IP
                async with session.post(
                    f"{base}/api/cameras",
                    json={
                        "name": "Living Room Phone",
                        "room": "Living Room",
                        "ip_address": "127.0.0.1",
                        "port": p_sim1,
                        "sensitivity": "high",
                        "motion_assist_enabled": True,
                    },
                ) as r:
                    assert r.status == 201
                    cam1 = await r.json()

                # Connect Phone 2 by DIFFERENT IP (127.0.0.2)
                async with session.post(
                    f"{base}/api/cameras",
                    json={
                        "name": "Bedroom Phone",
                        "room": "Bedroom",
                        "ip_address": "127.0.0.2",
                        "port": p_sim2,
                        "sensitivity": "medium",
                    },
                ) as r:
                    assert r.status == 201
                    cam2 = await r.json()

                # Connect Phone 3 via WebSocket /ws/phone-node (Browser Camera mode)
                phone3_ws = await session.ws_connect(f"ws://127.0.0.1:{p_app}/ws/phone-node")
                await phone3_ws.send_json(
                    {
                        "type": "start_camera",
                        "room": "Kitchen",
                        "name": "Kitchen Browser Phone",
                        "battery": 87,
                    }
                )
                sample_frame = feed1.render_jpeg()
                await phone3_ws.send_bytes(sample_frame)
                await asyncio.sleep(1.2)

                # Verify all 3 phone cameras are online simultaneously
                async with session.get(f"{base}/api/cameras") as r:
                    data = await r.json()
                    cams = data["cameras"]
                    assert len(cams) == 3, f"Expected 3 cameras, got {len(cams)}"
                    online_names = {c["name"] for c in cams if c["status"] == "online"}
                    assert len(online_names) == 3, f"Not all online: {cams}"
                    ips = {c["ip_address"] for c in cams}
                    assert "127.0.0.1" in ips and "127.0.0.2" in ips
                print("[PASS] 3 multi-IP phone cameras connected and online simultaneously")

                # Verify snapshots work for all 3
                for cam in cams:
                    async with session.get(f"{base}/api/cameras/{cam['id']}/snapshot") as r:
                        assert r.status == 200
                        assert r.headers["Content-Type"] == "image/jpeg"
                        body = await r.read()
                        assert len(body) > 1000

                # Trigger Fall on Phone 1 via Motion-Contour Assist (toggle simulator fall)
                feed1.force_fallen = True
                fall_evt = None
                for _ in range(45):
                    await asyncio.sleep(0.25)
                    evts = db.list_events(event_type="fall")
                    if evts:
                        fall_evt = evts[0]
                        break
                assert fall_evt is not None, "Expected fall event from Phone 1 simulator fall"
                assert fall_evt["phone_ip"] == "127.0.0.1"
                assert fall_evt["photo_url"] is not None

                # Wait briefly for post-event H.264 video clip finalization
                for _ in range(30):
                    await asyncio.sleep(0.3)
                    updated_evt = db.get_event(fall_evt["id"])
                    if updated_evt and updated_evt.get("video_url"):
                        fall_evt = updated_evt
                        break
                assert fall_evt["video_url"] is not None, "Video clip was not attached to fall event"
                feed1.force_fallen = False
                print(f"[PASS] Fall alert captured photo + MP4 clip ({fall_evt['duration_sec']}s)")

                # Trigger manual test fall on Phone 2
                async with session.post(f"{base}/api/cameras/{cam2['id']}/test-fall") as r:
                    assert r.status == 201
                    test_evt = await r.json()
                    assert test_evt["phone_ip"] == "127.0.0.2"

                # Verify WebSocket alerts were pushed
                alert_msgs = [m for m in ws_alerts if m.get("type") == "new_alert"]
                assert len(alert_msgs) >= 2, f"Expected >=2 WS alerts, got {len(alert_msgs)}"

                # Playwright Real Browser E2E Verification
                from playwright.async_api import async_playwright

                async with async_playwright() as p:
                    browser = await p.chromium.launch(
                        channel="chrome",
                        headless=True,
                        args=[
                            "--use-fake-device-for-media-stream",
                            "--use-fake-ui-for-media-stream",
                        ],
                    )
                    context = await browser.new_context(
                        viewport={"width": 1280, "height": 840},
                        permissions=["camera"],
                    )
                    page = await context.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda err: errors.append(str(err)))
                    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

                    await page.goto(base, wait_until="domcontentloaded")
                    await page.wait_for_selector("#homeCameraGrid .cam-card")
                    cards_count = await page.locator("#homeCameraGrid .cam-card").count()
                    assert cards_count == 3, f"Expected 3 camera cards in UI, got {cards_count}"

                    # Verify hero status shows Fall Detected
                    hero_title = await page.locator("#heroTitle").inner_text()
                    assert "Fall Detected" in hero_title, f"Unexpected hero title: {hero_title}"

                    # Open Event Detail from Recent Events and verify photo + H.264 video playback
                    await page.locator("#homeEventList .event-item").first.click()
                    await page.wait_for_selector("#eventDetailModal.open")
                    # Wait for video element if present (or click Video tab)
                    if await page.locator("#btnShowVideo").is_visible():
                        await page.locator("#btnShowVideo").click()
                        await page.wait_for_selector("#detailMediaBox video")
                        can_play = await page.evaluate(
                            """async () => {
                                const v = document.querySelector('#detailMediaBox video');
                                if (!v) return false;
                                if (v.readyState >= 1) return true;
                                return await new Promise(res => {
                                    v.addEventListener('loadedmetadata', () => res(true), {once: true});
                                    v.addEventListener('error', () => res(false), {once: true});
                                    setTimeout(() => res(v.readyState >= 1), 3000);
                                });
                            }"""
                        )
                        assert can_play, "Browser could not decode/play the recorded H.264 MP4 clip"
                    await page.locator("#detailResolveBtn").click()
                    await page.wait_for_timeout(400)

                    # Switch to Cameras tab and open Connect Phone IP modal
                    await page.locator('.desktop-nav [data-tab="cameras"]').click()
                    await page.locator("#btnOpenConnectModal").click()
                    await page.wait_for_selector("#connectModal.open")
                    await page.fill("#ipInput", "127.0.0.1")
                    await page.fill("#portInput", str(p_sim1))
                    await page.click("#btnTestProbe")
                    await page.wait_for_selector("#probePreviewImg[src^='data:image']")
                    await page.click("#connectModal [data-close='connectModal']")

                    # Switch to Settings tab and add emergency contact
                    await page.locator('.desktop-nav [data-tab="settings"]').click()
                    await page.click("#btnAddContact")
                    await page.fill("#newContactName", "Dr. Nadia")
                    await page.fill("#newContactRel", "Family Doctor")
                    await page.fill("#newContactPhone", "+60129998888")
                    await page.click("#btnSaveContact")
                    await page.wait_for_timeout(400)
                    contacts_txt = await page.locator("#contactsList").inner_text()
                    assert "Dr. Nadia" in contacts_txt

                    # Verify /phone-camera page loads AND live getUserMedia streams as 4th phone camera!
                    phone_page = await context.new_page()
                    await phone_page.set_viewport_size({"width": 393, "height": 852})
                    phone_page.on("pageerror", lambda err: errors.append(str(err)))
                    phone_page.on("dialog", lambda d: (errors.append(d.message), asyncio.create_task(d.dismiss())))
                    await phone_page.goto(f"{base}/phone-camera", wait_until="domcontentloaded")
                    await phone_page.wait_for_function(
                        "() => document.getElementById('phoneIpBadge').innerText.includes('127.0.0.1')",
                        timeout=8000,
                    )
                    await phone_page.fill("#nodeRoom", "Hallway")
                    await phone_page.fill("#nodeName", "Hallway Browser Phone")
                    await phone_page.click("#btnStart")
                    await phone_page.wait_for_selector("#connPill.online", timeout=10000)
                    conn_txt = await phone_page.locator("#connPill").inner_text()
                    assert "Streaming" in conn_txt, f"Expected phone node streaming, got: {conn_txt}"

                    # Verify 4th camera appeared on dashboard & test Larger View Modal + Cinema switcher
                    await page.bring_to_front()
                    if await page.locator("#fallAlertModal.open").is_visible():
                        await page.click("#fallAlertModal [data-close='fallAlertModal']")
                    await page.locator('.desktop-nav [data-tab="cameras"]').click()
                    await page.wait_for_timeout(600)
                    cams_after = await page.locator("#camerasFullGrid .cam-card").count()
                    assert cams_after == 4, f"Expected 4 cameras after browser phone start, got {cams_after}"

                    await page.locator("#camerasFullGrid .cam-card [data-cam-action='expand']").first.click()
                    await page.wait_for_selector("#cameraExpandModal.open")
                    stepper_count = await page.locator("#expandStateStepper .state-step").count()
                    assert stepper_count == 4, f"Expected 4 state-machine steps, got {stepper_count}"
                    await page.click("#cameraExpandModal [data-close='cameraExpandModal']")

                    await page.locator('.desktop-nav [data-tab="home"]').click()
                    await page.click('[data-cam-size="cinema"]')
                    has_cinema = await page.locator("#homeCameraGrid.size-cinema").count()
                    assert has_cinema == 1, "Expected cinema camera size class applied"

                    assert not errors, f"Browser console/page errors: {errors}"
                    await browser.close()
                print("[PASS] Playwright Chrome E2E UI + H.264 MP4 + Live Phone Node verification passed with 0 errors")

                await phone3_ws.close()
                await caregiver_ws.close()
                ws_task.cancel()
                try:
                    await ws_task
                except asyncio.CancelledError:
                    pass
        finally:
            # Cleanup test events media files from CAPTURES_DIR
            for evt in db.list_events():
                db.delete_event(evt["id"], CAPTURES_DIR)
            await runner.cleanup()
            await sim1.cleanup()
            await sim2.cleanup()
            db.close()


async def main() -> None:
    test_state_machine_and_pose_analysis()
    await test_qr_encoder()
    test_media_recorder()
    await test_full_stack_and_browser()
    print("\nALL E2E TESTS PASSED!")


if __name__ == "__main__":
    asyncio.run(main())
