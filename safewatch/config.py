"""Central configuration for SafeWatch Elderly Home Monitoring System."""

from __future__ import annotations

import os
import socket
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CAPTURES_DIR = DATA_DIR / "captures"
DATASETS_DIR = DATA_DIR / "datasets"
GMDCSA24_DIR = DATASETS_DIR / "gmdcsa24"
CERTS_DIR = BASE_DIR / "certs"
MODELS_DIR = BASE_DIR / "models"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
DB_PATH = DATA_DIR / "safewatch.db"

HTTP_HOST = os.environ.get("SAFEWATCH_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("SAFEWATCH_HTTP_PORT", "8000"))
HTTPS_PORT = int(os.environ.get("SAFEWATCH_HTTPS_PORT", "8443"))

MODEL_PATH = MODELS_DIR / "yolo11n-pose.pt"
if not MODEL_PATH.exists() and (BASE_DIR / "yolo11n-pose.pt").exists():
    MODEL_PATH = BASE_DIR / "yolo11n-pose.pt"

# GMDCSA-24 Dataset Reference
GMDCSA24_DOI = "https://doi.org/10.5281/zenodo.13354453"
GMDCSA24_ZENODO_ZIP_URL = (
    "https://zenodo.org/api/records/13354453/files/"
    "ekramalam/GMDCSA24-A-Dataset-for-Human-Fall-Detection-in-Videos-v2.1.zip/content"
)
GMDCSA24_GITHUB_RAW = (
    "https://raw.githubusercontent.com/ekramalam/"
    "GMDCSA24-A-Dataset-for-Human-Fall-Detection-in-Videos/master"
)

# Inference & recording settings
RING_BUFFER_SECONDS = 12
TARGET_CAPTURE_FPS = 10
POST_EVENT_RECORD_SECONDS = 3.0
DEFAULT_COOLDOWN_SECONDS = 15.0
INFERENCE_IMGSZ = 480

# Sensitivity presets -> state-machine & pose thresholds (calibrated with GMDCSA-24)
SENSITIVITY_PRESETS = {
    "low": {
        "falling_angle_deg": 38.0,
        "lying_torso_angle_deg": 60.0,
        "lying_body_angle_deg": 64.0,
        "upright_max_deg": 26.0,
        "aspect_ratio": 1.30,
        "confirm_seconds": 1.6,
        "slow_confirm_multiplier": 8.5,
        "drop_norm_thresh": 0.30,
        "drop_speed_thresh": 0.34,
    },
    "medium": {
        "falling_angle_deg": 32.0,
        "lying_torso_angle_deg": 52.0,
        "lying_body_angle_deg": 56.0,
        "upright_max_deg": 28.0,
        "aspect_ratio": 1.15,
        "confirm_seconds": 1.0,
        "slow_confirm_multiplier": 7.5,
        "drop_norm_thresh": 0.27,
        "drop_speed_thresh": 0.28,
    },
    "high": {
        "falling_angle_deg": 26.0,
        "lying_torso_angle_deg": 45.0,
        "lying_body_angle_deg": 50.0,
        "upright_max_deg": 30.0,
        "aspect_ratio": 1.00,
        "confirm_seconds": 0.55,
        "slow_confirm_multiplier": 3.0,
        "drop_norm_thresh": 0.16,
        "drop_speed_thresh": 0.20,
    },
}


def get_lan_ips() -> list[str]:
    """Return non-loopback IPv4 addresses for this host, preferring Wi-Fi/LAN."""
    ips: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            primary = s.getsockname()[0]
            if primary and not primary.startswith("127."):
                ips.append(primary)
    except OSError:
        pass

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and not ip.startswith("169.254.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass

    if not ips:
        ips.append("127.0.0.1")
    return ips


def ensure_directories() -> None:
    """Ensure runtime directories exist."""
    for d in (DATA_DIR, CAPTURES_DIR, DATASETS_DIR, GMDCSA24_DIR, CERTS_DIR, MODELS_DIR):
        d.mkdir(parents=True, exist_ok=True)
