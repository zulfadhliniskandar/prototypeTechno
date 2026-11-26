"""SafeWatch — Multi-Phone AI Elderly Home Monitoring Web Server."""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from aiohttp import WSMsgType, web

from safewatch.camera_manager import CameraManager, StandbyPhoneNode
from safewatch.config import (
    CAPTURES_DIR,
    GMDCSA24_DIR,
    GMDCSA24_DOI,
    HTTP_HOST,
    HTTP_PORT,
    HTTPS_PORT,
    STATIC_DIR,
    TEMPLATES_DIR,
    ensure_directories,
    get_lan_ips,
)
from safewatch.database import Database, utc_now_iso
from safewatch.fall_detector import PoseEngine
from safewatch.gmdcsa24 import discover_dataset_clips
from safewatch.network_discovery import probe_phone_camera, scan_local_network
from safewatch.qr import generate_qr_data_uri
from safewatch.ssl_helper import ensure_ssl_context


def _placeholder_jpeg(title: str, subtitle: str, bg_bgr=(32, 46, 58)) -> bytes:
    img = np.full((360, 640, 3), bg_bgr, dtype=np.uint8)
    cv2.putText(img, title, (40, 165), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(img, subtitle, (40, 205), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (185, 215, 215), 1, cv2.LINE_AA)
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return jpg.tobytes() if ok else b""


def create_app(
    db: Database | None = None,
    pose_engine: PoseEngine | None = None,
    https_enabled: bool = False,
    http_port: int = HTTP_PORT,
    https_port: int = HTTPS_PORT,
) -> web.Application:
    ensure_directories()
    database = db or Database()
    engine = pose_engine or PoseEngine()
    cam_manager = CameraManager(database, engine)

    app = web.Application(client_max_size=15 * 1024 * 1024)
    app["db"] = database
    app["camera_manager"] = cam_manager
    app["https_enabled"] = https_enabled
    app["http_port"] = http_port
    app["https_port"] = https_port

    async def on_startup(_app: web.Application) -> None:
        await cam_manager.startup()

    async def on_cleanup(_app: web.Application) -> None:
        await cam_manager.shutdown()
        if db is None:
            database.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # ---------- HTML Pages ----------

    async def index_page(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def phone_camera_page(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(TEMPLATES_DIR / "phone_camera.html")

    # ---------- System Info & Bootstrap ----------

    def _build_system_info(request: web.Request | None = None) -> dict[str, Any]:
        lan_ips = get_lan_ips()
        primary_ip = lan_ips[0]
        if app["https_enabled"]:
            phone_url = f"https://{primary_ip}:{app['https_port']}/phone-camera"
        else:
            phone_url = f"http://{primary_ip}:{app['http_port']}/phone-camera"
        qr_uri = ""
        try:
            qr_uri = generate_qr_data_uri(phone_url, box_size=7, border=2)
        except Exception:
            qr_uri = ""
        return {
            "lan_ips": lan_ips,
            "primary_ip": primary_ip,
            "http_port": app["http_port"],
            "https_port": app["https_port"],
            "https_enabled": app["https_enabled"],
            "phone_camera_url": phone_url,
            "local_phone_camera_url": f"http://localhost:{app['http_port']}/phone-camera",
            "phone_qr_data_uri": qr_uri,
            "client_ip": request.remote if request else None,
        }

    async def get_system_info(request: web.Request) -> web.Response:
        return web.json_response(_build_system_info(request))

    async def get_gmdcsa24_info(_request: web.Request) -> web.Response:
        clips = discover_dataset_clips(GMDCSA24_DIR)
        return web.json_response(
            {
                "dataset": "GMDCSA-24",
                "doi": GMDCSA24_DOI,
                "clips": [
                    {
                        "label": f"{c.subject} / {c.category} / {c.filename}",
                        "category": c.category,
                        "description": c.description,
                        "video_path": str(c.video_path),
                    }
                    for c in clips
                ],
            }
        )

    async def get_state(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "system": _build_system_info(request),
                "caregiver": database.get_caregiver_profile(),
                "resident": database.get_resident_profile(),
                "contacts": database.list_contacts(),
                "cameras": cam_manager.list_cameras_enriched(),
                "standby_nodes": cam_manager.list_standby_nodes(),
                "events": database.list_events(limit=120),
                "settings": database.get_settings(),
            }
        )

    # ---------- Profiles, Consent & Settings ----------

    async def update_caregiver(request: web.Request) -> web.Response:
        data = await request.json()
        updated = database.update_caregiver_profile(data)
        return web.json_response(updated)

    async def update_resident(request: web.Request) -> web.Response:
        data = await request.json()
        updated = database.update_resident_profile(data)
        await cam_manager.broadcast_caregiver({"type": "resident_updated", "resident": updated})
        return web.json_response(updated)

    async def update_settings(request: web.Request) -> web.Response:
        data = await request.json()
        updated = database.update_settings(data)
        return web.json_response(updated)

    async def purge_events(request: web.Request) -> web.Response:
        days = int(database.get_settings().get("retention_days", 30))
        try:
            body = await request.json()
            if "days" in body:
                days = int(body["days"])
        except Exception:
            pass
        count = database.purge_old_events(days, CAPTURES_DIR)
        return web.json_response({"purged": count})

    # ---------- Emergency Contacts ----------

    async def list_contacts(_request: web.Request) -> web.Response:
        return web.json_response(database.list_contacts())

    async def create_contact(request: web.Request) -> web.Response:
        data = await request.json()
        if not data.get("name") or not data.get("phone"):
            return web.json_response({"error": "Name and phone are required"}, status=400)
        return web.json_response(database.create_contact(data), status=201)

    async def update_contact(request: web.Request) -> web.Response:
        cid = request.match_info["id"]
        data = await request.json()
        updated = database.update_contact(cid, data)
        if not updated:
            return web.json_response({"error": "Contact not found"}, status=404)
        return web.json_response(updated)

    async def delete_contact(request: web.Request) -> web.Response:
        cid = request.match_info["id"]
        if not database.delete_contact(cid):
            return web.json_response({"error": "Contact not found"}, status=404)
        return web.json_response({"deleted": True})

    # ---------- Phone Cameras (Multi-IP Connection & Discovery) ----------

    async def list_cameras(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "cameras": cam_manager.list_cameras_enriched(),
                "standby_nodes": cam_manager.list_standby_nodes(),
            }
        )

    async def probe_camera(request: web.Request) -> web.Response:
        data = await request.json()
        ip_or_url = (data.get("ip_address") or data.get("stream_url") or "").strip()
        if not ip_or_url:
            return web.json_response({"error": "IP address or URL is required"}, status=400)
        port = int(data.get("port") or 8080)
        preset = data.get("preset", "auto")
        username = data.get("username", "")
        password = data.get("password", "")
        result = await probe_phone_camera(
            ip_or_url, port=port, username=username, password=password, preset=preset
        )
        return web.json_response(result)

    async def scan_network(request: web.Request) -> web.Response:
        subnet = None
        try:
            body = await request.json()
            subnet = body.get("subnet")
        except Exception:
            pass
        found = await scan_local_network(subnet=subnet)
        return web.json_response({"discovered": found})

    async def add_camera(request: web.Request) -> web.Response:
        data = await request.json()
        name = (data.get("name") or "").strip()
        room = (data.get("room") or "Living Room").strip()
        source_type = data.get("source_type", "ip_camera")
        ip_input = (data.get("ip_address") or "").strip()
        stream_url = (data.get("stream_url") or "").strip()
        port = int(data.get("port") or 8080)
        preset = data.get("preset", "auto")
        username = data.get("username", "")
        password = data.get("password", "")

        if source_type == "webcam":
            cam_index = stream_url or "0"
            ip_val = "127.0.0.1"
            cam_name = name or f"Local Camera {cam_index}"
            payload = {
                **data,
                "name": cam_name,
                "room": room,
                "source_type": "webcam",
                "ip_address": ip_val,
                "port": 0,
                "stream_url": cam_index,
            }
        elif source_type == "video_file":
            if not stream_url:
                return web.json_response({"error": "Video file path is required"}, status=400)
            payload = {
                **data,
                "name": name or f"GMDCSA-24 Feed ({Path(stream_url).name})",
                "room": room,
                "source_type": "video_file",
                "ip_address": ip_input or "127.0.0.1",
                "port": 0,
                "stream_url": stream_url,
            }
        elif source_type == "phone_node":
            if not ip_input:
                return web.json_response({"error": "Phone IP address is required"}, status=400)
            payload = {
                **data,
                "name": name or f"{room} Phone",
                "room": room,
                "source_type": "phone_node",
                "ip_address": ip_input,
                "port": 443,
                "stream_url": f"wss://{get_lan_ips()[0]}:{app['https_port']}/ws/phone-node",
            }
        else:
            target = stream_url or ip_input
            if not target:
                return web.json_response(
                    {"error": "Phone IP address or stream URL is required"}, status=400
                )
            # Probe first if stream_url wasn't explicitly chosen, or normalize IP/URL
            probe = await probe_phone_camera(
                target, port=port, username=username, password=password, preset=preset, timeout=2.0
            )
            resolved_ip = probe["ip_address"]
            resolved_port = probe["port"]
            resolved_url = stream_url if stream_url.startswith(("http://", "https://", "rtsp://")) else probe["stream_url"]
            payload = {
                **data,
                "name": name or f"{room} Phone ({resolved_ip})",
                "room": room,
                "source_type": source_type,
                "ip_address": resolved_ip,
                "port": resolved_port,
                "stream_url": resolved_url,
                "status": "online" if probe["reachable"] else "connecting",
            }

        cam = await cam_manager.add_camera(payload)
        return web.json_response(cam, status=201)

    async def update_camera(request: web.Request) -> web.Response:
        cam_id = request.match_info["id"]
        data = await request.json()
        updated = await cam_manager.update_camera(cam_id, data)
        if not updated:
            return web.json_response({"error": "Camera not found"}, status=404)
        return web.json_response(updated)

    async def delete_camera(request: web.Request) -> web.Response:
        cam_id = request.match_info["id"]
        if not await cam_manager.remove_camera(cam_id):
            return web.json_response({"error": "Camera not found"}, status=404)
        return web.json_response({"deleted": True})

    async def test_camera_fall(request: web.Request) -> web.Response:
        cam_id = request.match_info["id"]
        worker = cam_manager.workers.get(cam_id)
        if not worker:
            return web.json_response({"error": "Camera not found"}, status=404)
        event = await worker.trigger_manual_test()
        return web.json_response(event, status=201)

    async def camera_snapshot(request: web.Request) -> web.Response:
        cam_id = request.match_info["id"]
        worker = cam_manager.workers.get(cam_id)
        if not worker:
            return web.json_response({"error": "Camera not found"}, status=404)
        jpg = worker.latest_annotated_jpeg
        if not jpg:
            jpg = _placeholder_jpeg(
                worker.cfg["name"],
                f"IP: {worker.cfg['ip_address']} | Status: {worker.cfg['status'].upper()}",
            )
        return web.Response(body=jpg, content_type="image/jpeg")

    async def camera_stream(request: web.Request) -> web.StreamResponse:
        cam_id = request.match_info["id"]
        worker = cam_manager.workers.get(cam_id)
        if not worker:
            return web.Response(status=404, text="Camera not found")

        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            },
        )
        await resp.prepare(request)
        try:
            while True:
                if cam_id not in cam_manager.workers:
                    break
                jpg = worker.latest_annotated_jpeg
                if not jpg:
                    jpg = _placeholder_jpeg(
                        worker.cfg["name"],
                        f"Waiting for phone stream ({worker.cfg['ip_address']})...",
                    )
                header = (
                    f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n"
                ).encode("ascii")
                await resp.write(header + jpg + b"\r\n")
                await asyncio.sleep(0.10)
        except (asyncio.CancelledError, ConnectionResetError, Exception):
            pass
        return resp

    # ---------- Events & Media ----------

    async def list_events(request: web.Request) -> web.Response:
        q = request.rel_url.query
        events = database.list_events(
            event_type=q.get("type"),
            status=q.get("status"),
            camera_id=q.get("camera_id"),
            limit=int(q.get("limit", "150")),
        )
        return web.json_response(events)

    async def get_event(request: web.Request) -> web.Response:
        eid = request.match_info["id"]
        evt = database.get_event(eid)
        if not evt:
            return web.json_response({"error": "Event not found"}, status=404)
        return web.json_response(evt)

    async def update_event(request: web.Request) -> web.Response:
        eid = request.match_info["id"]
        data = await request.json()
        updated = database.update_event(eid, data)
        if not updated:
            return web.json_response({"error": "Event not found"}, status=404)
        await cam_manager.broadcast_caregiver({"type": "event_updated", "event": updated})
        return web.json_response(updated)

    async def add_event_log(request: web.Request) -> web.Response:
        eid = request.match_info["id"]
        data = await request.json()
        action = (data.get("action") or "").strip()
        if not action:
            return web.json_response({"error": "action is required"}, status=400)
        actor = data.get("actor", "Caregiver")
        updated = database.add_response_log(eid, actor, action)
        if not updated:
            return web.json_response({"error": "Event not found"}, status=404)
        await cam_manager.broadcast_caregiver({"type": "event_updated", "event": updated})
        return web.json_response(updated)

    async def delete_event(request: web.Request) -> web.Response:
        eid = request.match_info["id"]
        if not database.delete_event(eid, CAPTURES_DIR):
            return web.json_response({"error": "Event not found"}, status=404)
        await cam_manager.broadcast_caregiver({"type": "event_deleted", "event_id": eid})
        return web.json_response({"deleted": True})

    async def serve_media(request: web.Request) -> web.StreamResponse:
        filename = Path(request.match_info["filename"]).name
        file_path = CAPTURES_DIR / filename
        if not file_path.exists():
            return web.Response(status=404, text="Media not found")
        resp = web.FileResponse(file_path)
        if request.rel_url.query.get("download") == "1":
            resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp

    # ---------- WebSockets ----------

    async def ws_caregiver(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=25.0)
        await ws.prepare(request)
        cam_manager.caregiver_sockets.add(ws)
        try:
            await ws.send_json(
                {
                    "type": "hello",
                    "cameras": cam_manager.list_cameras_enriched(),
                    "standby_nodes": cam_manager.list_standby_nodes(),
                }
            )
            async for msg in ws:
                if msg.type == WSMsgType.TEXT and msg.data == "ping":
                    await ws.send_str("pong")
        finally:
            cam_manager.caregiver_sockets.discard(ws)
        return ws

    async def ws_phone_node(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024, heartbeat=25.0)
        await ws.prepare(request)

        client_ip = request.remote or "unknown"
        # Respect X-Forwarded-For or custom test header if provided locally
        node_id = f"node-{uuid.uuid4().hex[:8]}"
        node = StandbyPhoneNode(
            node_id=node_id,
            ip_address=client_ip,
            device_label=f"Phone ({client_ip})",
            ws=ws,
        )
        await cam_manager.register_standby_node(node)
        await ws.send_json(
            {
                "type": "registered",
                "node_id": node_id,
                "ip_address": client_ip,
                "camera_id": node.camera_id,
            }
        )

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    mtype = payload.get("type")
                    if mtype == "hello":
                        if payload.get("reported_ip"):
                            node.ip_address = str(payload["reported_ip"])
                        node.device_label = payload.get("label") or f"Phone ({node.ip_address})"
                        if payload.get("battery") is not None:
                            node.battery_level = int(payload["battery"])
                        await cam_manager.broadcast_caregiver(
                            {"type": "standby_nodes", "nodes": cam_manager.list_standby_nodes()}
                        )
                    elif mtype == "start_camera":
                        room = (payload.get("room") or "Living Room").strip()
                        name = (payload.get("name") or f"{room} Phone").strip()
                        if payload.get("battery") is not None:
                            node.battery_level = int(payload["battery"])
                        cam = await cam_manager.activate_phone_node(node, name=name, room=room)
                        await ws.send_json({"type": "camera_active", "camera": cam})
                    elif mtype == "battery":
                        lvl = payload.get("level")
                        if lvl is not None:
                            node.battery_level = int(lvl)
                            if node.camera_id:
                                await cam_manager.update_camera(
                                    node.camera_id, {"battery_level": node.battery_level}
                                )
                    elif mtype == "stop_camera":
                        if node.camera_id:
                            w = cam_manager.workers.get(node.camera_id)
                            if w:
                                w._update_status("offline")
                elif msg.type == WSMsgType.BINARY:
                    if not node.camera_id:
                        # Auto-activate if phone started pushing frames directly
                        await cam_manager.activate_phone_node(
                            node, name=node.device_label, room="Living Room"
                        )
                    worker = cam_manager.workers.get(node.camera_id) if node.camera_id else None
                    if worker:
                        arr = np.frombuffer(msg.data, dtype=np.uint8)
                        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if frame is not None:
                            worker.submit_frame(frame)
        finally:
            await cam_manager.unregister_standby_node(node_id)
        return ws

    # ---------- Service Worker & PWA Manifest ----------

    async def service_worker_file(_request: web.Request) -> web.Response:
        sw_path = STATIC_DIR / "sw.js"
        if not sw_path.exists():
            return web.Response(status=404, text="sw.js not found")
        content = sw_path.read_text(encoding="utf-8")
        return web.Response(
            body=content,
            content_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    async def manifest_file(_request: web.Request) -> web.Response:
        m_path = STATIC_DIR / "manifest.json"
        if not m_path.exists():
            return web.Response(status=404, text="manifest.json not found")
        content = m_path.read_text(encoding="utf-8")
        return web.Response(
            body=content,
            content_type="application/manifest+json",
            headers={"Cache-Control": "no-cache"},
        )

    # ---------- Notification Endpoints ----------

    async def subscribe_notification(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        endpoint = (payload.get("endpoint") or "").strip()
        if not endpoint:
            return web.json_response({"error": "endpoint is required"}, status=400)
        keys = payload.get("keys") or {}
        p256dh = keys.get("p256dh", "")
        auth = keys.get("auth", "")
        ua = request.headers.get("User-Agent", "")
        device_name = payload.get("device_name") or "Caregiver Smartphone / Browser"
        sub = database.save_notification_subscription(
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=ua,
            device_name=device_name,
        )
        return web.json_response({"status": "subscribed", "subscription": sub})

    async def get_notification_status(_request: web.Request) -> web.Response:
        subs = database.list_notification_subscriptions()
        return web.json_response(
            {
                "supported": True,
                "subscriptions_count": len(subs),
                "subscriptions": subs,
                "push_mode": "service_worker_notification",
            }
        )

    async def trigger_test_notification(_request: web.Request) -> web.Response:
        res = database.get_resident_profile()
        res_name = res.get("name") or "Resident"
        test_evt_id = f"test-{uuid.uuid4().hex[:8]}"
        cams = database.list_cameras()
        room = cams[0]["room"] if cams else "Living Room"
        test_event = {
            "id": test_evt_id,
            "camera_id": cams[0]["id"] if cams else None,
            "camera_name": cams[0]["name"] if cams else "Phone Camera (Test)",
            "room": room,
            "phone_ip": cams[0]["ip_address"] if cams else "192.168.1.100",
            "event_type": "fall",
            "confidence": 0.98,
            "photo_filename": None,
            "video_filename": None,
            "duration_sec": 4.5,
            "status": "pending",
            "notes": "Simulated lockscreen / background test fall alert",
            "created_at": utc_now_iso(),
            "resolved_at": None,
            "photo_url": "/static/icon-512.png",
            "video_url": None,
            "response_logs": [],
            "is_test": True,
            "notification_title": f"🚨 SafeWatch Fall Alert: {res_name}!",
            "notification_body": f"CRITICAL: Fall detected in {room} (98% confidence). Tap to view live camera.",
        }
        await cam_manager.broadcast_caregiver(
            {"type": "new_alert", "event": test_event, "is_test": True}
        )
        return web.json_response({"status": "dispatched", "event": test_event})

    # ---------- Route Table ----------

    app.router.add_get("/", index_page)
    app.router.add_get("/sw.js", service_worker_file)
    app.router.add_get("/manifest.json", manifest_file)
    app.router.add_get("/phone-camera", phone_camera_page)
    app.router.add_get("/camera-node", phone_camera_page)
    app.router.add_get("/favicon.ico", lambda _r: web.Response(status=204))

    app.router.add_get("/api/system/info", get_system_info)
    app.router.add_get("/api/dataset/gmdcsa24", get_gmdcsa24_info)
    app.router.add_get("/api/state", get_state)
    app.router.add_put("/api/caregiver", update_caregiver)
    app.router.add_put("/api/resident", update_resident)
    app.router.add_put("/api/settings", update_settings)
    app.router.add_post("/api/events/purge", purge_events)

    app.router.add_get("/api/notifications/status", get_notification_status)
    app.router.add_post("/api/notifications/subscribe", subscribe_notification)
    app.router.add_post("/api/notifications/test", trigger_test_notification)

    app.router.add_get("/api/contacts", list_contacts)
    app.router.add_post("/api/contacts", create_contact)
    app.router.add_put("/api/contacts/{id}", update_contact)
    app.router.add_delete("/api/contacts/{id}", delete_contact)

    app.router.add_get("/api/cameras", list_cameras)
    app.router.add_post("/api/cameras/probe", probe_camera)
    app.router.add_post("/api/cameras/scan", scan_network)
    app.router.add_post("/api/cameras", add_camera)
    app.router.add_put("/api/cameras/{id}", update_camera)
    app.router.add_delete("/api/cameras/{id}", delete_camera)
    app.router.add_post("/api/cameras/{id}/test-fall", test_camera_fall)
    app.router.add_get("/api/cameras/{id}/snapshot", camera_snapshot)
    app.router.add_get("/api/cameras/{id}/stream", camera_stream)

    app.router.add_get("/api/events", list_events)
    app.router.add_get("/api/events/{id}", get_event)
    app.router.add_put("/api/events/{id}", update_event)
    app.router.add_post("/api/events/{id}/logs", add_event_log)
    app.router.add_delete("/api/events/{id}", delete_event)
    app.router.add_get("/media/{filename}", serve_media)

    app.router.add_get("/ws/caregiver", ws_caregiver)
    app.router.add_get("/ws/phone-node", ws_phone_node)

    app.router.add_static("/static", STATIC_DIR)
    return app


async def run_servers() -> None:
    ensure_directories()
    ssl_ctx = ensure_ssl_context()
    https_enabled = ssl_ctx is not None
    app = create_app(https_enabled=https_enabled)

    runner = web.AppRunner(app)
    await runner.setup()

    http_site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)
    await http_site.start()

    https_site = None
    if ssl_ctx is not None:
        https_site = web.TCPSite(runner, HTTP_HOST, HTTPS_PORT, ssl_context=ssl_ctx)
        await https_site.start()

    lan_ips = get_lan_ips()
    primary_ip = lan_ips[0]
    print("\n" + "=" * 68)
    print("  SafeWatch — Multi-Phone AI Elderly Home Monitoring System")
    print("=" * 68)
    print(f"  Caregiver Dashboard (Local) : http://localhost:{HTTP_PORT}")
    print(f"  Caregiver Dashboard (LAN)   : http://{primary_ip}:{HTTP_PORT}")
    if https_site:
        print(f"  Phone Camera Node (HTTPS)   : https://{primary_ip}:{HTTPS_PORT}/phone-camera")
    print(f"  Phone Camera Node (Local)   : http://localhost:{HTTP_PORT}/phone-camera")
    print("  Supports IP Webcam / DroidCam / RTSP / SafeWatch Phone Node")
    print("=" * 68 + "\n")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(run_servers())
    except KeyboardInterrupt:
        sys.exit(0)
