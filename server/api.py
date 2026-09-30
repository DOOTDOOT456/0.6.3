"""
PolyTrack community leaderboard backend (unofficial, for this site only).

Implements the subset of the PolyTrack "v6" server API used by the game client
(see main.bundle.js, class wf). The client validates responses strictly, so
field names/types must match exactly.

Storage: Supabase (Postgres) via its REST API, configured with SUPABASE_URL and
SUPABASE_SERVICE_ROLE_KEY env vars. Falls back to a local JSON file
(server_data/leaderboard.json) when Supabase is not configured, so the sandbox
preview still works offline.
"""

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "server_data")
DATA_FILE = os.path.join(DATA_DIR, "leaderboard.json")
TOKEN_RE = re.compile(r"^[A-Za-z0-9]{1,128}$")
TRACK_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,80}$")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_KEY)

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Storage backends
# ---------------------------------------------------------------------------

class SupabaseStore:
    def __init__(self, url, key):
        self.url = url
        self.key = key

    def _req(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url + path,
            headers={
                "apikey": self.key,
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            data=data,
            method=method,
        )
        try:
            r = urllib.request.urlopen(req, timeout=15)
            raw = r.read().decode()
            return r.status, json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                return e.code, json.loads(body)
            except Exception:
                return e.code, body

    # users ---------------------------------------------------------------

    def upsert_user(self, user_id, nickname, country, car_style):
        payload = {
            "user_id": user_id,
            "nickname": nickname,
            "country_code": country,
            "car_style": car_style,
        }
        self._req(
            "/rest/v1/lb_users?on_conflict=user_id",
            "POST",
            payload,
        )

    def get_user(self, user_id):
        status, rows = self._req(f"/rest/v1/lb_users?user_id=eq.{user_id}&limit=1")
        if status != 200 or not rows:
            return None
        row = rows[0]
        return {
            "nickname": row["nickname"],
            "countryCode": row.get("country_code"),
            "carStyle": row.get("carStyle", row.get("car_style", "")),
            "isVerifier": False,
        }

    # entries -------------------------------------------------------------

    def get_entries(self, track_id):
        status, rows = self._req(
            f"/rest/v1/lb_entries?track_id=eq.{track_id}&order=frames.asc,id.asc&limit=1000"
        )
        if status != 200:
            return []
        return [self._row_to_entry(r) for r in rows]

    @staticmethod
    def _row_to_entry(r):
        return {
            "id": int(r["id"]),
            "userId": r["user_id"],
            "nickname": r["nickname"],
            "frames": int(r["frames"]),
            "time": r["time"],
            "carStyle": r["car_style"],
            "countryCode": r.get("country_code"),
            "recording": r.get("recording", ""),
        }

    def upsert_entry(self, track_id, user_id, nickname, country, car_style, frames, recording, now_iso):
        # Try to load the existing entry first so we can report position change.
        status, rows = self._req(
            f"/rest/v1/lb_entries?track_id=eq.{track_id}&user_id=eq.{user_id}&limit=1"
        )
        existing = rows[0] if status == 200 and rows else None
        payload = {
            "track_id": track_id,
            "user_id": user_id,
            "nickname": nickname,
            "country_code": country,
            "car_style": car_style,
            "frames": frames,
            "time": now_iso,
            "recording": recording,
        }
        if existing is not None:
            if frames < int(existing["frames"]):
                status, updated = self._req(
                    f"/rest/v1/lb_entries?id=eq.{existing['id']}", "PATCH", payload
                )
                if status in (200, 204):
                    entry = self._row_to_entry(updated[0] if updated else {**existing, **payload})
                    return entry
            entry = self._row_to_entry(existing)
            return entry
        status, created = self._req("/rest/v1/lb_entries", "POST", payload)
        if status in (200, 201) and created:
            return self._row_to_entry(created[0])
        return None

    def get_recording(self, entry_id):
        status, rows = self._req(f"/rest/v1/lb_entries?id=eq.{entry_id}&limit=1")
        if status != 200 or not rows:
            return None
        return self._row_to_entry(rows[0])


class JsonStore:
    """Fallback storage: single JSON file (single-process sandbox use)."""

    def _load(self):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data.setdefault("nextId", 1)
        data.setdefault("users", {})
        data.setdefault("entries", {})
        return data

    def _save(self, data):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, DATA_FILE)

    def upsert_user(self, user_id, nickname, country, car_style):
        with _lock:
            data = self._load()
            data["users"].setdefault(
                user_id, {"nickname": nickname, "countryCode": country, "carStyle": car_style}
            )
            self._save(data)

    def get_user(self, user_id):
        with _lock:
            data = self._load()
            u = data["users"].get(user_id)
        if u is None:
            return None
        return {
            "nickname": u["nickname"],
            "countryCode": u.get("countryCode"),
            "carStyle": u["carStyle"],
            "isVerifier": False,
        }

    def get_entries(self, track_id):
        with _lock:
            data = self._load()
            entries = list(data["entries"].get(track_id, []))
        entries.sort(key=lambda e: (e["frames"], e["id"]))
        return entries

    def upsert_entry(self, track_id, user_id, nickname, country, car_style, frames, recording, now_iso):
        with _lock:
            data = self._load()
            data["users"].setdefault(
                user_id, {"nickname": nickname, "countryCode": country, "carStyle": car_style}
            )
            entries = data["entries"].setdefault(track_id, [])
            existing = next((e for e in entries if e["userId"] == user_id), None)
            if existing is not None:
                if frames < existing["frames"]:
                    existing.update(
                        frames=frames,
                        recording=recording,
                        time=now_iso,
                        nickname=nickname,
                        countryCode=country,
                        carStyle=car_style,
                    )
                entry = dict(existing)
            else:
                entry = {
                    "id": data["nextId"],
                    "userId": user_id,
                    "nickname": nickname,
                    "frames": frames,
                    "time": now_iso,
                    "carStyle": car_style,
                    "countryCode": country,
                    "recording": recording,
                }
                data["nextId"] += 1
                entries.append(entry)
            entries.sort(key=lambda e: (e["frames"], e["id"]))
            self._save(data)
        return entry

    def get_recording(self, entry_id):
        with _lock:
            data = self._load()
        for track_entries in data["entries"].values():
            for e in track_entries:
                if e["id"] == entry_id:
                    return e
        return None


store = SupabaseStore(SUPABASE_URL, SUPABASE_KEY) if USE_SUPABASE else JsonStore()


def user_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]


def nick_ok(nick):
    return 1 <= len(nick) <= 50


def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def position_of(entries, entry_id):
    for pos, e in enumerate(entries, start=1):
        if e["id"] == entry_id:
            return pos
    return 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PolyTrackCommunityLB/2.0"

    # ---------- helpers ----------

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status=200):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _form(self):
        raw = self._read_body().decode("utf-8", "replace")
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def log_message(self, fmt, *args):
        pass

    # ---------- routing ----------

    def do_OPTIONS(self):
        self._send_empty(204)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.lstrip("/") or "index.html"
        parts = path.split("/")

        if len(parts) >= 2 and parts[0] == "api" and parts[1] == "v6":
            endpoint = parts[2] if len(parts) > 2 else ""
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            try:
                if endpoint == "leaderboard":
                    return self._get_leaderboard(qs)
                if endpoint == "leaderboardUserEntry":
                    return self._get_user_entry(qs)
                if endpoint == "recordings":
                    return self._get_recordings(qs)
                if endpoint == "user":
                    return self._get_user(qs)
                if endpoint == "iceServers":
                    return self._send_json([])
                if endpoint == "trackOfTheWeek":
                    return self._send_json({"serverTime": iso(), "current": None})
            except Exception:
                return self._send_json({"error": "internal"}, 500)
            return self._send_json({"error": "not found"}, 404)

        return self._serve_static(path)

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        parts = parsed.path.lstrip("/").split("/")
        if len(parts) >= 2 and parts[0] == "api" and parts[1] == "v6":
            endpoint = parts[2] if len(parts) > 2 else ""
            form = self._form()
            try:
                if endpoint == "leaderboard":
                    return self._post_leaderboard(form)
                if endpoint == "user":
                    return self._post_user(form)
            except Exception:
                return self._send_json({"error": "internal"}, 500)
        return self._send_json({"error": "not found"}, 404)

    # ---------- endpoints ----------

    def _get_leaderboard(self, qs):
        def one(name):
            return (qs.get(name) or [""])[0]

        track_id = one("trackId")
        skip = int(one("skip") or 0)
        amount = min(int(one("amount") or 20), 100)
        user_token_hash = one("userTokenHash")

        entries = store.get_entries(track_id)
        total = len(entries)
        page = entries[skip: skip + amount]
        user_entry = None
        if user_token_hash:
            for pos, e in enumerate(entries, start=1):
                if e["userId"] == user_token_hash:
                    user_entry = {"position": pos, "frames": e["frames"], "id": e["id"]}
                    break
        out = {
            "total": total,
            "entries": [
                {
                    "id": e["id"],
                    "userId": e["userId"],
                    "nickname": e["nickname"],
                    "frames": e["frames"],
                    "time": e["time"],
                    "carStyle": e["carStyle"],
                    "verifiedState": 1,
                    "countryCode": e.get("countryCode"),
                }
                for e in page
            ],
            "userEntry": user_entry,
        }
        self._send_json(out)

    def _get_user_entry(self, qs):
        track_id = (qs.get("trackId") or [""])[0]
        token_hash = (qs.get("userTokenHash") or [""])[0]
        entries = store.get_entries(track_id)
        for pos, e in enumerate(entries, start=1):
            if e["userId"] == token_hash:
                return self._send_json({"position": pos, "frames": e["frames"], "id": e["id"]})
        self._send_json(None)

    def _get_recordings(self, qs):
        ids_raw = (qs.get("ids") or [""])[0]
        ids = [s for s in ids_raw.split(",") if s]
        out = []
        for entry_id in ids:
            try:
                n = int(entry_id)
            except ValueError:
                out.append(None)
                continue
            e = store.get_recording(n)
            if e is None:
                out.append(None)
                continue
            out.append(
                {
                    "recording": e["recording"],
                    "verifiedState": 1,
                    "frames": e["frames"],
                    "carStyle": e["carStyle"],
                }
            )
        self._send_json(out)

    def _get_user(self, qs):
        token = (qs.get("userToken") or [""])[0]
        u = store.get_user(user_hash(token))
        if u is None:
            return self._send_json(None)
        self._send_json(u)

    def _post_user(self, form):
        token = (form.get("userToken") or [""])[0]
        nickname = (form.get("nickname") or [""])[0]
        country = (form.get("countryCode") or [None])[0]
        car_style = (form.get("carStyle") or [""])[0]
        if not TOKEN_RE.match(token) or not nick_ok(nickname):
            return self._send_empty(400)
        store.upsert_user(user_hash(token), nickname, country, car_style)
        self._send_empty(200)

    def _post_leaderboard(self, form):
        token = (form.get("userToken") or [""])[0]
        nickname = (form.get("nickname") or [""])[0]
        country = (form.get("countryCode") or [None])[0]
        car_style = (form.get("carStyle") or [""])[0]
        track_id = (form.get("trackId") or [""])[0]
        try:
            frames = int((form.get("frames") or ["0"])[0])
        except ValueError:
            frames = 0
        recording = (form.get("recording") or [""])[0]
        if not TOKEN_RE.match(token) or not nick_ok(nickname) or not TRACK_ID_RE.match(track_id):
            return self._send_json({"error": "bad request"}, 400)
        if frames <= 0 or len(recording) <= 0 or len(recording) >= 60000:
            return self._send_json({"error": "bad request"}, 400)

        user_id = user_hash(token)
        store.upsert_user(user_id, nickname, country, car_style)
        entries_before = store.get_entries(track_id)
        prev_pos = 0
        for e in entries_before:
            if e["userId"] == user_id:
                prev_pos = position_of(entries_before, e["id"])
                break

        entry = store.upsert_entry(
            track_id, user_id, nickname, country, car_style, frames, recording, iso()
        )
        if entry is None:
            return self._send_json({"error": "internal"}, 500)
        entries_after = store.get_entries(track_id)
        new_pos = position_of(entries_after, entry["id"])
        if new_pos == 0:
            new_pos = len(entries_after)
        self._send_json(
            {
                "uploadId": entry["id"],
                "previousPosition": prev_pos if prev_pos else new_pos,
                "newPosition": new_pos,
            }
        )

    # ---------- static files ----------

    def _serve_static(self, path):
        if path == "index.html" or path == "":
            path = "index.html"
        full = os.path.realpath(os.path.join(ROOT, path))
        if not full.startswith(ROOT) or not os.path.isfile(full):
            return self._send_empty(404)
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript",
            ".json": "application/json",
            ".css": "text/css",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".wasm": "application/wasm",
            ".glb": "model/gltf-binary",
            ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg",
            ".ttf": "font/ttf",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
            ".track": "application/octet-stream",
        }.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            return self._send_empty(404)
        # Game assets are content-hashed webpack bundles, but index.html and
        # the main bundle change on patch - keep everything revalidatable.
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    mode = "Supabase" if USE_SUPABASE else "local JSON"
    print(f"PolyTrack community server on 0.0.0.0:{port} (storage: {mode})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
