"""Phone IP camera endpoint prober and local Wi-Fi scanner."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
from typing import Any
from urllib.parse import urlparse

import aiohttp
import cv2
import numpy as np

from .config import get_lan_ips


def build_candidate_urls(
    ip_or_url: str,
    port: int = 8080,
    preset: str = "auto",
) -> tuple[str, int, list[tuple[str, str]]]:
    """
    Parse user-provided IP address or full URL and build candidate (mode, url) endpoints.
    Returns (ip_address, port, candidates).
    """
    raw = ip_or_url.strip()
    if re.match(r"^(https?|rtsp)://", raw, re.IGNORECASE):
        parsed = urlparse(raw)
        host = parsed.hostname or raw
        parsed_port = parsed.port or (554 if parsed.scheme == "rtsp" else (443 if parsed.scheme == "https" else 80))
        scheme = parsed.scheme.lower()
        if scheme == "rtsp":
            return host, parsed_port, [("rtsp", raw)]
        # If user gave bare http://ip:port without path, expand standard phone paths
        if parsed.path in ("", "/"):
            base = f"{scheme}://{host}:{parsed_port}"
            return host, parsed_port, _preset_candidates(base, host, parsed_port, preset)
        mode = "snapshot" if raw.lower().endswith((".jpg", ".jpeg", ".png")) else "mjpeg"
        return host, parsed_port, [(mode, raw)]

    # IP[:port] format
    if ":" in raw and raw.count(":") == 1:
        host, port_str = raw.split(":", 1)
        host = host.strip()
        try:
            port = int(port_str.strip())
        except ValueError:
            pass
    else:
        host = raw

    if preset == "droidcam" and port == 8080:
        port = 4747
    elif preset == "rtsp" and port == 8080:
        port = 8554

    base = f"http://{host}:{port}"
    return host, port, _preset_candidates(base, host, port, preset)


def _preset_candidates(base: str, host: str, port: int, preset: str) -> list[tuple[str, str]]:
    if preset == "ip_webcam":
        return [
            ("mjpeg", f"{base}/video"),
            ("mjpeg", f"{base}/videofeed"),
            ("snapshot", f"{base}/shot.jpg"),
        ]
    if preset == "droidcam":
        return [
            ("mjpeg", f"http://{host}:{port}/video"),
            ("mjpeg", f"http://{host}:{port}/mjpegfeed"),
            ("mjpeg", f"http://{host}:4747/video"),
        ]
    if preset == "rtsp":
        return [
            ("rtsp", f"rtsp://{host}:{port}/live"),
            ("rtsp", f"rtsp://{host}:{port}/h264_ulaw.sdp"),
            ("rtsp", f"rtsp://{host}:8554/live"),
        ]
    # Auto preset checks IP Webcam, DroidCam, generic MJPEG, and snapshot endpoints
    return [
        ("mjpeg", f"{base}/video"),
        ("mjpeg", f"{base}/videofeed"),
        ("snapshot", f"{base}/shot.jpg"),
        ("mjpeg", f"{base}/mjpegfeed"),
        ("mjpeg", f"{base}/stream.mjpg"),
        ("mjpeg", f"http://{host}:4747/video"),
        ("rtsp", f"rtsp://{host}:{port}/live"),
    ]


def _extract_first_jpeg_from_bytes(buf: bytes) -> bytes | None:
    start = buf.find(b"\xff\xd8")
    if start == -1:
        return None
    end = buf.find(b"\xff\xd9", start + 2)
    if end == -1:
        return None
    return buf[start : end + 2]


def _decode_jpeg_b64(jpeg_bytes: bytes) -> tuple[str | None, int, int]:
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None, 0, 0
    h, w = img.shape[:2]
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{b64}", w, h


async def probe_phone_camera(
    ip_or_url: str,
    port: int = 8080,
    username: str = "",
    password: str = "",
    preset: str = "auto",
    timeout: float = 2.5,
) -> dict[str, Any]:
    """Probe a phone IP address to find a working stream URL and capture a preview frame."""
    host, parsed_port, candidates = build_candidate_urls(ip_or_url, port=port, preset=preset)
    auth = aiohttp.BasicAuth(username, password) if username else None
    client_timeout = aiohttp.ClientTimeout(total=timeout, connect=min(timeout, 1.5))

    last_err = "Could not connect to phone camera endpoint"

    async with aiohttp.ClientSession(timeout=client_timeout, auth=auth) as session:
        for mode, url in candidates:
            if mode == "rtsp":
                # Probe RTSP synchronously in thread
                ok, frame = await asyncio.to_thread(_probe_rtsp_frame, url, username, password)
                if ok and frame is not None:
                    ok_enc, jpg = cv2.imencode(".jpg", frame)
                    if ok_enc:
                        preview, w, h = _decode_jpeg_b64(jpg.tobytes())
                        return {
                            "reachable": True,
                            "ip_address": host,
                            "port": parsed_port,
                            "stream_url": url,
                            "mode": "rtsp",
                            "preview_data_url": preview,
                            "width": w,
                            "height": h,
                            "error": None,
                        }
                continue

            try:
                async with session.get(url, ssl=False) as resp:
                    if resp.status >= 400:
                        last_err = f"HTTP {resp.status} at {url}"
                        continue
                    ctype = resp.headers.get("Content-Type", "").lower()
                    buf = b""
                    if mode == "snapshot" or "image/jpeg" in ctype:
                        buf = await resp.content.read(1_500_000)
                        jpg = _extract_first_jpeg_from_bytes(buf) or buf
                        preview, w, h = _decode_jpeg_b64(jpg)
                        if preview:
                            return {
                                "reachable": True,
                                "ip_address": host,
                                "port": parsed_port,
                                "stream_url": url,
                                "mode": "snapshot",
                                "preview_data_url": preview,
                                "width": w,
                                "height": h,
                                "error": None,
                            }
                    else:
                        # Read chunks until we see at least one full JPEG frame
                        async for chunk in resp.content.iter_chunked(8192):
                            buf += chunk
                            jpg = _extract_first_jpeg_from_bytes(buf)
                            if jpg:
                                preview, w, h = _decode_jpeg_b64(jpg)
                                if preview:
                                    return {
                                        "reachable": True,
                                        "ip_address": host,
                                        "port": parsed_port,
                                        "stream_url": url,
                                        "mode": "mjpeg",
                                        "preview_data_url": preview,
                                        "width": w,
                                        "height": h,
                                        "error": None,
                                    }
                            if len(buf) > 1_500_000:
                                break
            except Exception as exc:
                last_err = str(exc) or exc.__class__.__name__

    return {
        "reachable": False,
        "ip_address": host,
        "port": parsed_port,
        "stream_url": candidates[0][1] if candidates else "",
        "mode": "unknown",
        "preview_data_url": None,
        "width": 0,
        "height": 0,
        "error": last_err,
    }


def _probe_rtsp_frame(url: str, username: str = "", password: str = ""):
    if username and "://" in url and "@" not in url:
        scheme, rest = url.split("://", 1)
        url = f"{scheme}://{username}:{password}@{rest}"
    cap = cv2.VideoCapture(url)
    try:
        if not cap.isOpened():
            return False, None
        ok, frame = cap.read()
        return ok, frame
    finally:
        cap.release()


async def _check_tcp(ip: str, port: int, timeout: float = 0.30) -> tuple[str, int] | None:
    try:
        conn = asyncio.open_connection(ip, port)
        reader, writer = await asyncio.wait_for(conn, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return (ip, port)
    except Exception:
        return None


async def scan_local_network(
    subnet: str | None = None,
    ports: tuple[int, ...] = (8080, 4747, 8081),
) -> list[dict[str, Any]]:
    """Scan local Wi-Fi subnet for phones serving IP Webcam / DroidCam streams."""
    if not subnet:
        lan_ip = get_lan_ips()[0]
        net = ipaddress.IPv4Network(f"{lan_ip}/24", strict=False)
    else:
        net = ipaddress.IPv4Network(subnet if "/" in subnet else f"{subnet}/24", strict=False)

    hosts = [str(h) for h in list(net.hosts())[:254]]
    sem = asyncio.Semaphore(80)

    async def _bounded_check(ip: str, port: int):
        async with sem:
            return await _check_tcp(ip, port)

    tasks = [_bounded_check(ip, p) for ip in hosts for p in ports]
    results = await asyncio.gather(*tasks)
    open_targets = [r for r in results if r is not None]

    discovered: list[dict[str, Any]] = []
    for ip, port in open_targets:
        probe = await probe_phone_camera(ip, port=port, timeout=1.5)
        if probe["reachable"]:
            discovered.append(probe)
    return discovered
