"""SQLite persistence layer for SafeWatch Elderly Home Monitoring."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DB_PATH


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Database:
    """Thread-safe SQLite database wrapper."""

    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._init_schema()
            self._seed_defaults()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS caregiver_profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                full_name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS resident_profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                name TEXT NOT NULL,
                relationship TEXT NOT NULL,
                phone TEXT NOT NULL,
                address TEXT NOT NULL,
                medical_notes TEXT NOT NULL,
                consent_agreed INTEGER NOT NULL DEFAULT 0,
                consent_timestamp TEXT,
                consent_pin TEXT,
                consent_rooms_json TEXT NOT NULL DEFAULT '[]',
                master_paused INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS emergency_contacts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                relationship TEXT NOT NULL,
                phone TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 1,
                notify_on_fall INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cameras (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                room TEXT NOT NULL,
                source_type TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                port INTEGER NOT NULL DEFAULT 8080,
                stream_url TEXT NOT NULL,
                username TEXT DEFAULT '',
                password TEXT DEFAULT '',
                fall_detection_enabled INTEGER NOT NULL DEFAULT 1,
                inactivity_enabled INTEGER NOT NULL DEFAULT 1,
                motion_enabled INTEGER NOT NULL DEFAULT 0,
                motion_assist_enabled INTEGER NOT NULL DEFAULT 0,
                sensitivity TEXT NOT NULL DEFAULT 'medium',
                inactivity_minutes INTEGER NOT NULL DEFAULT 30,
                roi_json TEXT NOT NULL DEFAULT '{"x":0,"y":0,"w":1,"h":1}',
                paused INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'offline',
                last_seen_at TEXT,
                last_event_at TEXT,
                battery_level INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                camera_id TEXT,
                camera_name TEXT NOT NULL,
                room TEXT NOT NULL,
                phone_ip TEXT NOT NULL,
                event_type TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.0,
                photo_filename TEXT,
                video_filename TEXT,
                duration_sec REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'pending',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                FOREIGN KEY (camera_id) REFERENCES cameras(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS response_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notification_subscriptions (
                endpoint TEXT PRIMARY KEY,
                p256dh TEXT DEFAULT '',
                auth TEXT DEFAULT '',
                user_agent TEXT DEFAULT '',
                device_name TEXT DEFAULT 'Caregiver Device',
                created_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def _seed_defaults(self) -> None:
        now = utc_now_iso()
        cur = self._conn.execute("SELECT COUNT(*) FROM caregiver_profile")
        if cur.fetchone()[0] == 0:
            self._conn.execute(
                """
                INSERT INTO caregiver_profile (id, full_name, email, phone, updated_at)
                VALUES (1, ?, ?, ?, ?)
                """,
                ("Faris Rezza", "faris@niutech.my", "+60123456789", now),
            )

        cur = self._conn.execute("SELECT COUNT(*) FROM resident_profile")
        if cur.fetchone()[0] == 0:
            self._conn.execute(
                """
                INSERT INTO resident_profile (
                    id, name, relationship, phone, address, medical_notes,
                    consent_agreed, consent_timestamp, consent_pin, consent_rooms_json,
                    master_paused, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Puan Aminah",
                    "Parent",
                    "+60198765432",
                    "No. 14, Jalan Harmoni, Shah Alam",
                    "Mild hypertension; independent mobility at home.",
                    1,
                    now,
                    "1234",
                    json.dumps(["Living Room", "Hallway", "Bedroom"]),
                    0,
                    now,
                ),
            )

        cur = self._conn.execute("SELECT COUNT(*) FROM emergency_contacts")
        if cur.fetchone()[0] == 0:
            contacts = [
                ("contact-1", "Malaysia Emergency (MERS 999)", "Emergency Service", "999", 1, 1, now),
                ("contact-2", "Encik Hakim (Next-Door Neighbour)", "Neighbour", "+60172233445", 2, 1, now),
            ]
            self._conn.executemany(
                """
                INSERT INTO emergency_contacts (id, name, relationship, phone, priority, notify_on_fall, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                contacts,
            )

        default_settings = {
            "retention_days": "30",
            "auto_delete": "true",
            "alert_sound": "true",
            "quiet_hours_enabled": "false",
            "quiet_start": "23:00",
            "quiet_end": "07:00",
            "cooldown_seconds": "15",
            "show_skeleton_overlay": "true",
        }
        for k, v in default_settings.items():
            self._conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )
        self._conn.commit()

    # ---------- Profiles & Settings ----------

    def get_caregiver_profile(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM caregiver_profile WHERE id = 1").fetchone()
            return dict(row) if row else {}

    def update_caregiver_profile(self, data: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock:
            curr = self.get_caregiver_profile()
            self._conn.execute(
                """
                UPDATE caregiver_profile
                SET full_name = ?, email = ?, phone = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    data.get("full_name", curr.get("full_name", "Caregiver")),
                    data.get("email", curr.get("email", "")),
                    data.get("phone", curr.get("phone", "")),
                    now,
                ),
            )
            self._conn.commit()
            return self.get_caregiver_profile()

    def get_resident_profile(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM resident_profile WHERE id = 1").fetchone()
            if not row:
                return {}
            d = dict(row)
            d["consent_agreed"] = bool(d["consent_agreed"])
            d["master_paused"] = bool(d["master_paused"])
            try:
                d["consent_rooms"] = json.loads(d.pop("consent_rooms_json") or "[]")
            except json.JSONDecodeError:
                d["consent_rooms"] = []
            d.pop("consent_pin", None)
            return d

    get_resident = get_resident_profile

    def update_resident_profile(self, data: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock:
            row = self._conn.execute("SELECT * FROM resident_profile WHERE id = 1").fetchone()
            curr = dict(row) if row else {}
            consent_agreed = int(bool(data.get("consent_agreed", curr.get("consent_agreed", 0))))
            consent_ts = curr.get("consent_timestamp")
            if "consent_agreed" in data:
                consent_ts = now if consent_agreed else None
            rooms_json = (
                json.dumps(data["consent_rooms"])
                if "consent_rooms" in data
                else curr.get("consent_rooms_json", "[]")
            )
            master_paused = int(bool(data.get("master_paused", curr.get("master_paused", 0))))
            self._conn.execute(
                """
                UPDATE resident_profile
                SET name = ?, relationship = ?, phone = ?, address = ?, medical_notes = ?,
                    consent_agreed = ?, consent_timestamp = ?, consent_pin = ?,
                    consent_rooms_json = ?, master_paused = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    data.get("name", curr.get("name", "")),
                    data.get("relationship", curr.get("relationship", "Parent")),
                    data.get("phone", curr.get("phone", "")),
                    data.get("address", curr.get("address", "")),
                    data.get("medical_notes", curr.get("medical_notes", "")),
                    consent_agreed,
                    consent_ts,
                    data.get("consent_pin", curr.get("consent_pin", "1234")),
                    rooms_json,
                    master_paused,
                    now,
                ),
            )
            self._conn.commit()
            return self.get_resident_profile()

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
            out: dict[str, Any] = {}
            for r in rows:
                val = r["value"]
                if val in ("true", "false"):
                    out[r["key"]] = val == "true"
                elif val.isdigit():
                    out[r["key"]] = int(val)
                else:
                    out[r["key"]] = val
            return out

    def update_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for k, v in data.items():
                if isinstance(v, bool):
                    s = "true" if v else "false"
                else:
                    s = str(v)
                self._conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, s),
                )
            self._conn.commit()
            return self.get_settings()

    # ---------- Emergency Contacts ----------

    def list_contacts(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM emergency_contacts ORDER BY priority ASC, created_at ASC"
            ).fetchall()
            return [{**dict(r), "notify_on_fall": bool(r["notify_on_fall"])} for r in rows]

    def create_contact(self, data: dict[str, Any]) -> dict[str, Any]:
        cid = f"contact-{uuid.uuid4().hex[:8]}"
        now = utc_now_iso()
        with self._lock:
            max_p = self._conn.execute(
                "SELECT COALESCE(MAX(priority), 0) FROM emergency_contacts"
            ).fetchone()[0]
            self._conn.execute(
                """
                INSERT INTO emergency_contacts (id, name, relationship, phone, priority, notify_on_fall, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cid,
                    data["name"].strip(),
                    data.get("relationship", "Family").strip(),
                    data["phone"].strip(),
                    int(data.get("priority", max_p + 1)),
                    int(bool(data.get("notify_on_fall", True))),
                    now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM emergency_contacts WHERE id = ?", (cid,)).fetchone()
            return {**dict(row), "notify_on_fall": bool(row["notify_on_fall"])}

    def update_contact(self, cid: str, data: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM emergency_contacts WHERE id = ?", (cid,)).fetchone()
            if not row:
                return None
            curr = dict(row)
            self._conn.execute(
                """
                UPDATE emergency_contacts
                SET name = ?, relationship = ?, phone = ?, priority = ?, notify_on_fall = ?
                WHERE id = ?
                """,
                (
                    data.get("name", curr["name"]),
                    data.get("relationship", curr["relationship"]),
                    data.get("phone", curr["phone"]),
                    int(data.get("priority", curr["priority"])),
                    int(bool(data.get("notify_on_fall", curr["notify_on_fall"]))),
                    cid,
                ),
            )
            self._conn.commit()
            updated = self._conn.execute("SELECT * FROM emergency_contacts WHERE id = ?", (cid,)).fetchone()
            return {**dict(updated), "notify_on_fall": bool(updated["notify_on_fall"])}

    def delete_contact(self, cid: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM emergency_contacts WHERE id = ?", (cid,))
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- Cameras (Phones) ----------

    @staticmethod
    def _row_to_camera(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for flag in (
            "fall_detection_enabled",
            "inactivity_enabled",
            "motion_enabled",
            "motion_assist_enabled",
            "paused",
        ):
            d[flag] = bool(d[flag])
        try:
            d["roi"] = json.loads(d.pop("roi_json") or '{"x":0,"y":0,"w":1,"h":1}')
        except json.JSONDecodeError:
            d["roi"] = {"x": 0, "y": 0, "w": 1, "h": 1}
        d["has_password"] = bool(d.get("password"))
        d.pop("password", None)
        return d

    def list_cameras(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM cameras ORDER BY created_at ASC").fetchall()
            return [self._row_to_camera(r) for r in rows]

    def get_camera(self, camera_id: str, include_secret: bool = False) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM cameras WHERE id = ?", (camera_id,)).fetchone()
            if not row:
                return None
            if include_secret:
                d = dict(row)
                for flag in (
                    "fall_detection_enabled",
                    "inactivity_enabled",
                    "motion_enabled",
                    "motion_assist_enabled",
                    "paused",
                ):
                    d[flag] = bool(d[flag])
                d["roi"] = json.loads(d.pop("roi_json") or '{"x":0,"y":0,"w":1,"h":1}')
                return d
            return self._row_to_camera(row)

    def create_camera(self, data: dict[str, Any]) -> dict[str, Any]:
        cam_id = data.get("id") or f"phone-{uuid.uuid4().hex[:8]}"
        now = utc_now_iso()
        roi = data.get("roi") or {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cameras (
                    id, name, room, source_type, ip_address, port, stream_url,
                    username, password, fall_detection_enabled, inactivity_enabled,
                    motion_enabled, motion_assist_enabled, sensitivity,
                    inactivity_minutes, roi_json, paused, status, last_seen_at,
                    battery_level, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cam_id,
                    data["name"].strip(),
                    data.get("room", "Living Room").strip(),
                    data.get("source_type", "ip_camera"),
                    data.get("ip_address", "").strip(),
                    int(data.get("port", 8080)),
                    data.get("stream_url", "").strip(),
                    data.get("username", "").strip(),
                    data.get("password", ""),
                    int(bool(data.get("fall_detection_enabled", True))),
                    int(bool(data.get("inactivity_enabled", True))),
                    int(bool(data.get("motion_enabled", False))),
                    int(bool(data.get("motion_assist_enabled", False))),
                    data.get("sensitivity", "medium"),
                    int(data.get("inactivity_minutes", 30)),
                    json.dumps(roi),
                    int(bool(data.get("paused", False))),
                    data.get("status", "connecting"),
                    data.get("last_seen_at"),
                    data.get("battery_level"),
                    now,
                ),
            )
            self._conn.commit()
            return self.get_camera(cam_id)  # type: ignore[return-value]

    def update_camera(self, camera_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            curr = self.get_camera(camera_id, include_secret=True)
            if not curr:
                return None
            roi_json = json.dumps(data["roi"]) if "roi" in data else json.dumps(curr["roi"])
            self._conn.execute(
                """
                UPDATE cameras
                SET name = ?, room = ?, source_type = ?, ip_address = ?, port = ?,
                    stream_url = ?, username = ?, password = ?,
                    fall_detection_enabled = ?, inactivity_enabled = ?,
                    motion_enabled = ?, motion_assist_enabled = ?,
                    sensitivity = ?, inactivity_minutes = ?, roi_json = ?,
                    paused = ?, status = ?, last_seen_at = ?, last_event_at = ?,
                    battery_level = ?
                WHERE id = ?
                """,
                (
                    data.get("name", curr["name"]),
                    data.get("room", curr["room"]),
                    data.get("source_type", curr["source_type"]),
                    data.get("ip_address", curr["ip_address"]),
                    int(data.get("port", curr["port"])),
                    data.get("stream_url", curr["stream_url"]),
                    data.get("username", curr["username"]),
                    data.get("password", curr.get("password", "")),
                    int(bool(data.get("fall_detection_enabled", curr["fall_detection_enabled"]))),
                    int(bool(data.get("inactivity_enabled", curr["inactivity_enabled"]))),
                    int(bool(data.get("motion_enabled", curr["motion_enabled"]))),
                    int(bool(data.get("motion_assist_enabled", curr["motion_assist_enabled"]))),
                    data.get("sensitivity", curr["sensitivity"]),
                    int(data.get("inactivity_minutes", curr["inactivity_minutes"])),
                    roi_json,
                    int(bool(data.get("paused", curr["paused"]))),
                    data.get("status", curr["status"]),
                    data.get("last_seen_at", curr["last_seen_at"]),
                    data.get("last_event_at", curr["last_event_at"]),
                    data.get("battery_level", curr["battery_level"]),
                    camera_id,
                ),
            )
            self._conn.commit()
            return self.get_camera(camera_id)

    def delete_camera(self, camera_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def find_camera_by_ip(self, ip_address: str, source_type: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if source_type:
                row = self._conn.execute(
                    "SELECT * FROM cameras WHERE ip_address = ? AND source_type = ?",
                    (ip_address, source_type),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM cameras WHERE ip_address = ?", (ip_address,)
                ).fetchone()
            return self._row_to_camera(row) if row else None

    # ---------- Events & Response Logs ----------

    def create_event(self, data: dict[str, Any]) -> dict[str, Any]:
        event_id = data.get("id") or f"evt-{uuid.uuid4().hex[:10]}"
        now = data.get("created_at") or utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO events (
                    id, camera_id, camera_name, room, phone_ip, event_type,
                    confidence, photo_filename, video_filename, duration_sec,
                    status, notes, created_at, resolved_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    data.get("camera_id"),
                    data["camera_name"],
                    data["room"],
                    data.get("phone_ip", ""),
                    data["event_type"],
                    float(data.get("confidence", 0.0)),
                    data.get("photo_filename"),
                    data.get("video_filename"),
                    float(data.get("duration_sec", 0.0)),
                    data.get("status", "pending"),
                    data.get("notes", ""),
                    now,
                    data.get("resolved_at"),
                ),
            )
            if data.get("camera_id"):
                self._conn.execute(
                    "UPDATE cameras SET last_event_at = ? WHERE id = ?",
                    (now, data["camera_id"]),
                )
            initial_action = data.get("initial_log") or (
                f"{data['event_type'].replace('_', ' ').title()} detected by AI "
                f"({data.get('confidence', 0):.0f}% confidence)"
            )
            self._conn.execute(
                "INSERT INTO response_logs (event_id, actor, action, created_at) VALUES (?, ?, ?, ?)",
                (event_id, "System AI", initial_action, now),
            )
            self._conn.commit()
            return self.get_event(event_id)  # type: ignore[return-value]

    def update_event(self, event_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if not row:
                return None
            curr = dict(row)
            status = data.get("status", curr["status"])
            resolved_at = curr["resolved_at"]
            if status in ("resolved", "false_alert") and not resolved_at:
                resolved_at = utc_now_iso()
            elif status == "pending":
                resolved_at = None

            self._conn.execute(
                """
                UPDATE events
                SET photo_filename = ?, video_filename = ?, duration_sec = ?,
                    status = ?, notes = ?, resolved_at = ?
                WHERE id = ?
                """,
                (
                    data.get("photo_filename", curr["photo_filename"]),
                    data.get("video_filename", curr["video_filename"]),
                    float(data.get("duration_sec", curr["duration_sec"])),
                    status,
                    data.get("notes", curr["notes"]),
                    resolved_at,
                    event_id,
                ),
            )
            if data.get("log_action"):
                self._conn.execute(
                    "INSERT INTO response_logs (event_id, actor, action, created_at) VALUES (?, ?, ?, ?)",
                    (
                        event_id,
                        data.get("log_actor", "Caregiver"),
                        data["log_action"],
                        utc_now_iso(),
                    ),
                )
            self._conn.commit()
            return self.get_event(event_id)

    def add_response_log(self, event_id: str, actor: str, action: str) -> dict[str, Any] | None:
        with self._lock:
            if not self._conn.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone():
                return None
            self._conn.execute(
                "INSERT INTO response_logs (event_id, actor, action, created_at) VALUES (?, ?, ?, ?)",
                (event_id, actor, action, utc_now_iso()),
            )
            self._conn.commit()
            return self.get_event(event_id)

    def _enrich_event(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["photo_url"] = f"/media/{d['photo_filename']}" if d.get("photo_filename") else None
        d["video_url"] = f"/media/{d['video_filename']}" if d.get("video_filename") else None
        logs = self._conn.execute(
            "SELECT id, actor, action, created_at FROM response_logs WHERE event_id = ? ORDER BY id ASC",
            (d["id"],),
        ).fetchall()
        d["response_logs"] = [dict(lg) for lg in logs]
        return d

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            return self._enrich_event(row) if row else None

    def list_events(
        self,
        event_type: str | None = None,
        status: str | None = None,
        camera_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._lock:
            clauses = []
            params: list[Any] = []
            if event_type and event_type != "all":
                clauses.append("event_type = ?")
                params.append(event_type)
            if status and status != "all":
                clauses.append("status = ?")
                params.append(status)
            if camera_id and camera_id != "all":
                clauses.append("camera_id = ?")
                params.append(camera_id)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            rows = self._conn.execute(
                f"SELECT * FROM events {where} ORDER BY created_at DESC LIMIT ?", params
            ).fetchall()
            return [self._enrich_event(r) for r in rows]

    def delete_event(self, event_id: str, captures_dir: Path | None = None) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT photo_filename, video_filename FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if not row:
                return False
            if captures_dir:
                for fn in (row["photo_filename"], row["video_filename"]):
                    if fn:
                        p = captures_dir / fn
                        if p.exists():
                            try:
                                p.unlink()
                            except OSError:
                                pass
            self._conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            self._conn.commit()
            return True

    def purge_old_events(self, retention_days: int, captures_dir: Path | None = None) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM events WHERE created_at < ?", (cutoff,)
            ).fetchall()
            count = 0
            for r in rows:
                if self.delete_event(r["id"], captures_dir):
                    count += 1
            return count

    # ---------- Notification Subscriptions ----------

    def save_notification_subscription(
        self,
        endpoint: str,
        p256dh: str = "",
        auth: str = "",
        user_agent: str = "",
        device_name: str = "Caregiver Device",
    ) -> dict[str, Any]:
        with self._lock:
            now = utc_now_iso()
            self._conn.execute(
                """
                INSERT INTO notification_subscriptions (endpoint, p256dh, auth, user_agent, device_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(endpoint) DO UPDATE SET
                    p256dh = excluded.p256dh,
                    auth = excluded.auth,
                    user_agent = excluded.user_agent,
                    device_name = excluded.device_name,
                    created_at = excluded.created_at
                """,
                (endpoint, p256dh, auth, user_agent, device_name, now),
            )
            self._conn.commit()
            return {
                "endpoint": endpoint,
                "p256dh": p256dh,
                "auth": auth,
                "user_agent": user_agent,
                "device_name": device_name,
                "created_at": now,
            }

    def list_notification_subscriptions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT endpoint, p256dh, auth, user_agent, device_name, created_at FROM notification_subscriptions ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_notification_subscription(self, endpoint: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM notification_subscriptions WHERE endpoint = ?", (endpoint,)
            )
            self._conn.commit()
            return cur.rowcount > 0

