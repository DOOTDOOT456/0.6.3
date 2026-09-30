"""
PolyTrack community leaderboard backend (unofficial, for this site only).

Implements the subset of the PolyTrack "v6" server API used by the game client
(see main.bundle.js, class wf). The client validates responses strictly, so
field names/types must match exactly.

Storage: JSON file (server/leaderboard.json) written atomically.
Server: stdlib-only (http.server) so it runs anywhere with plain python3.
"""

import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "server_data")
DATA_FILE = os.path.join(DATA_DIR, "leaderboard.json")
TOKEN_RE = re.compile(r"^[A-Za-z0-9]{1,128}$")
TRACK_ID_RE = re.compile(r"^[A-Za-z0-9]{1,80}$")

_lock = threading.Lock()


def _load():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.setdefault("nextId", 1)
    data.setdefault("users", {})
    data.setdefault("entries", {})
    return data


def _save(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, DATA_FILE)


def _user_hash(token):
    # Stable per-user id derived from the token. Token itself is never stored.
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]


def _nick_ok(nick):
    return 1 <= len(nick) <= 50


def _iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PolyTrackCommunityLB/1.0"

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

        # Serve the API under /api/v6/...
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
                    return self._get_ice_servers()
                if endpoint == "trackOfTheWeek":
                    return self._get_totw()
            except Exception:
                return self._send_json({"error": "internal"}, 500)
            return self._send_json({"error": "not found"}, 404)

        # Static files (same server, so the patched client can call /api/* same-origin)
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
        with _lock:
            data = _load()
            entries = data["entries"].get(track_id, [])
            entries = sorted(entries, key=lambda e: (e["frames"], e["id"]))
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
        with _lock:
            data = _load()
            entries = sorted(
                data["entries"].get(track_id, []), key=lambda e: (e["frames"], e["id"])
            )
            for pos, e in enumerate(entries, start=1):
                if e["userId"] == token_hash:
                    return self._send_json(
                        {"position": pos, "frames": e["frames"], "id": e["id"]}
                    )
        self._send_json(None)

    def _get_recordings(self, qs):
        ids_raw = (qs.get("ids") or [""])[0]
        ids = [s for s in ids_raw.split(",") if s]
        with _lock:
            data = _load()
            out = []
            for entry_id in ids:
                try:
                    n = int(entry_id)
                except ValueError:
                    out.append(None)
                    continue
                found = None
                for track_entries in data["entries"].values():
                    for e in track_entries:
                        if e["id"] == n:
                            found = e
                            break
                    if found:
                        break
                if found is None:
                    out.append(None)
                    continue
                out.append(
                    {
                        "recording": found["recording"],
                        "verifiedState": 1,
                        "frames": found["frames"],
                        "carStyle": found["carStyle"],
                    }
                )
        self._send_json(out)

    def _get_user(self, qs):
        token = (qs.get("userToken") or [""])[0]
        with _lock:
            data = _load()
            u = data["users"].get(_user_hash(token))
        if u is None:
            return self._send_json(None)
        self._send_json(
            {
                "nickname": u["nickname"],
                "countryCode": u.get("countryCode"),
                "carStyle": u["carStyle"],
                "isVerifier": False,
            }
        )

    def _post_user(self, form):
        token = (form.get("userToken") or [""])[0]
        nickname = (form.get("nickname") or [""])[0]
        country = (form.get("countryCode") or [None])[0]
        car_style = (form.get("carStyle") or [""])[0]
        if not TOKEN_RE.match(token) or not _nick_ok(nickname):
            return self._send_empty(400)
        with _lock:
            data = _load()
            data["users"][_user_hash(token)] = {
                "nickname": nickname,
                "countryCode": country,
                "carStyle": car_style,
            }
            _save(data)
        self._send_empty(200)

    def _post_leaderboard(self, form):
        token = (form.get("userToken") or [""])[0]
        nickname = (form.get("nickname") or [""])[0]
        country = (form.get("countryCode") or [None])[0]
        car_style = (form.get("carStyle") or [""])[0]
        track_id = (form.get("trackId") or [""])[0]
        frames = int((form.get("frames") or ["0"])[0])
        recording = (form.get("recording") or [""])[0]
        if not TOKEN_RE.match(token) or not _nick_ok(nickname) or not TRACK_ID_RE.match(track_id):
            return self._send_json({"error": "bad request"}, 400)
        if frames <= 0 or len(recording) <= 0 or len(recording) >= 60000:
            return self._send_json({"error": "bad request"}, 400)
        user_id = _user_hash(token)
        with _lock:
            data = _load()
            # remember profile so the user entry can be looked up later
            data["users"].setdefault(
                user_id,
                {"nickname": nickname, "countryCode": country, "carStyle": car_style},
            )
            entries = data["entries"].setdefault(track_id, [])
            existing = next((e for e in entries if e["userId"] == user_id), None)
            upload_id = None
            prev_pos = 0
            if existing is not None:
                prev_pos = self._position_of(entries, existing)
                if frames < existing["frames"]:
                    existing["frames"] = frames
                    existing["recording"] = recording
                    existing["time"] = _iso()
                    existing["nickname"] = nickname
                    existing["countryCode"] = country
                    existing["carStyle"] = car_style
                upload_id = existing["id"]
            else:
                upload_id = data["nextId"]
                data["nextId"] += 1
                entries.append(
                    {
                        "id": upload_id,
                        "userId": user_id,
                        "nickname": nickname,
                        "frames": frames,
                        "time": _iso(),
                        "carStyle": car_style,
                        "countryCode": country,
                        "recording": recording,
                    }
                )
            entries.sort(key=lambda e: (e["frames"], e["id"]))
            _save(data)
            new_pos = self._position_of(entries, next(e for e in entries if e["id"] == upload_id))
        self._send_json(
            {
                "uploadId": upload_id,
                "previousPosition": prev_pos if prev_pos else new_pos,
                "newPosition": new_pos,
            }
        )

    @staticmethod
    def _position_of(entries, entry):
        for pos, e in enumerate(entries, start=1):
            if e["id"] == entry["id"]:
                return pos
        return 0

    # ---------- stubs the client also calls ----------

    def _get_ice_servers(self):
        # Multiplayer uses WebRTC; hosting a TURN server is out of scope, so
        # return an empty list (the client handles this gracefully).
        self._send_json([])

    def _get_totw(self):
        # No track of the week for the community server: client accepts null current.
        self._send_json(
            {
                "serverTime": _iso(),
                "current": None,
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
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"PolyTrack community server listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
