"""Auto-generates self-signed SAN TLS certificates for HTTPS phone camera access."""

from __future__ import annotations

import shutil
import ssl
import subprocess
from pathlib import Path

from .config import CERTS_DIR, get_lan_ips


def _find_openssl() -> str | None:
    candidates = [
        shutil.which("openssl"),
        r"C:\Program Files\Git\mingw64\bin\openssl.exe",
        r"C:\Program Files\Git\usr\bin\openssl.exe",
        r"C:\Program Files (x86)\Git\usr\bin\openssl.exe",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def ensure_ssl_context(certs_dir: Path = CERTS_DIR) -> ssl.SSLContext | None:
    """Create (or reuse) a self-signed SAN certificate and return an SSLContext."""
    certs_dir.mkdir(parents=True, exist_ok=True)
    cert_path = certs_dir / "server.crt"
    key_path = certs_dir / "server.key"
    san_marker = certs_dir / "san_ips.txt"

    lan_ips = get_lan_ips()
    san_signature = ",".join(sorted(set(["127.0.0.1"] + lan_ips)))

    needs_regen = (
        not cert_path.exists()
        or not key_path.exists()
        or not san_marker.exists()
        or san_marker.read_text(encoding="utf-8").strip() != san_signature
    )

    if needs_regen:
        openssl_bin = _find_openssl()
        if not openssl_bin:
            return None
        alt_names = ["DNS:localhost", "IP:127.0.0.1"]
        for ip in lan_ips:
            if ip != "127.0.0.1":
                alt_names.append(f"IP:{ip}")

        cnf_path = certs_dir / "openssl.cnf"
        cnf_content = f"""[req]
default_bits = 2048
prompt = no
default_md = sha256
x509_extensions = v3_req
distinguished_name = dn

[dn]
CN = SafeWatch Local Hub
O = NiuTech SafeWatch

[v3_req]
subjectAltName = {",".join(alt_names)}
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
"""
        cnf_path.write_text(cnf_content, encoding="utf-8")
        try:
            subprocess.run(
                [
                    openssl_bin,
                    "req",
                    "-x509",
                    "-nodes",
                    "-days",
                    "825",
                    "-newkey",
                    "rsa:2048",
                    "-keyout",
                    str(key_path),
                    "-out",
                    str(cert_path),
                    "-config",
                    str(cnf_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            san_marker.write_text(san_signature, encoding="utf-8")
        except subprocess.CalledProcessError:
            return None

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        return ctx
    except Exception:
        return None
