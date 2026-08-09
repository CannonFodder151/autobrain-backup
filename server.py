#!/usr/bin/env python3
"""autobrain-backup web service.

Serves the backup GUI + JSON API over stdlib http.server, runs an hourly
scheduler in a background thread, and accepts pushed backups from the
autobrain-backup-agent via POST /ingest.

Config lives in a file on the docker host (mounted folder), editable in the
GUI at /config. Run state persists next to it for stats and health.
"""

import argparse
import base64
import hmac
import json
import secrets
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine import BackupEngine, BackupError, Config, State

STATIC_DIR = Path(__file__).resolve().parent / "static"
VERSION = "1.1.0"

SESSION_TTL = 24 * 3600


class App:
    def __init__(self, config_path, backup_dir):
        self.config = Config(config_path)
        self.state = State(Path(config_path).with_suffix(".state.json"))
        cfg = self.config.get()
        self.engine = BackupEngine(self.config, self.state, backup_dir or cfg.get("backup_dir"))
        self._sched_stop = threading.Event()
        self._sched = threading.Thread(target=self._scheduler, daemon=True)
        self._sched.start()
        self._sessions = {}

    def new_session(self):
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time() + SESSION_TTL
        return token

    def check_session(self, token):
        if not token:
            return False
        exp = self._sessions.get(token)
        if not exp:
            return False
        if time.time() > exp:
            self._sessions.pop(token, None)
            return False
        return True

    # --- scheduler ---
    def _scheduler(self):
        while not self._sched_stop.is_set():
            try:
                cfg = self.config.get()
                interval = max(60, int(cfg.get("schedule_interval", 3600)))
                st = self.state.get()
                last = None
                if st.get("last_run"):
                    try:
                        last = datetime.fromisoformat(st["last_run"])
                    except ValueError:
                        last = None
                now = datetime.now(timezone.utc)
                due = last is None or (now - last).total_seconds() >= interval
                if due:
                    if last is None:
                        # throttle a fresh install: first run shortly after boot
                        next_run = now + interval
                    else:
                        next_run = last + interval
                else:
                    next_run = last + interval
                self.state.update(next_run_at=next_run.isoformat())
                if due and not cfg.get("instance_url"):
                    # not configured yet; keep waiting, re-check each interval
                    pass
                elif due:
                    try:
                        self.engine.run_backup()
                    except BackupError as e:
                        self.state.update(last_status="fail", last_error=str(e), last_run=_utcnow_iso())
                        self.state.touch_counters(ok=False)
                        self.engine.alert_failure(e)
                    except Exception as e:  # defensive: never kill the scheduler
                        self.state.update(last_status="fail", last_error=f"unexpected: {e}", last_run=_utcnow_iso())
                        self.state.touch_counters(ok=False)
                try:
                    self.engine.rotate()
                except Exception:
                    pass
            except Exception:
                pass
            self._sched_stop.wait(60)

    def run_backup_now(self):
        try:
            name = self.engine.run_backup()
            return {"ok": True, "name": name}
        except BackupError as e:
            self.state.update(last_status="fail", last_error=str(e), last_run=_utcnow_iso())
            self.state.touch_counters(ok=False)
            self.engine.alert_failure(e)
            self.engine.alert_corruption(e) if "corrupt" in str(e).lower() else None
            return {"ok": False, "error": str(e)}

    def stop(self):
        self._sched_stop.set()


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


class Handler(BaseHTTPRequestHandler):
    server_version = "autobrain-backup/" + VERSION

    @property
    def app(self):
        return self.server.app

    # --- helpers ---
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def _authorized(self):
        cfg = self.app.config.get()
        key = cfg.get("gui_key") or ""
        user = cfg.get("gui_user") or ""
        if not key and not user:
            return True
        if key and (self.headers.get("X-Gui-Key") == key or self.headers.get("Authorization", "").replace("Bearer ", "") == key):
            return True
        if user and self.app.check_session(self._cookie("autobrain_session")):
            return True
        return False

    def _cookie(self, name):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _unauthorized(self):
        self._send(401, {"error": "unauthorized"})

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (datetime.now(timezone.utc).isoformat(), fmt % args))

    # --- routes ---
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                html = (STATIC_DIR / "index.html").read_text("utf-8")
                self._send(200, html, "text/html; charset=utf-8")
            except OSError:
                self._send(500, {"error": "missing index.html"})
            return
        if path == "/api/config":
            if not self._authorized():
                return self._unauthorized()
            return self._send(200, self.app.config.masked())
        if path == "/api/status":
            if not self._authorized():
                return self._unauthorized()
            return self._send(200, self.app.engine.status())
        if path == "/api/backups":
            if not self._authorized():
                return self._unauthorized()
            return self._send(200, self.app.engine.list_backups())
        if path == "/api/backup/download":
            if not self._authorized():
                return self._unauthorized()
            return self._download()
        if path == "/api/version":
            return self._send(200, {"version": VERSION, "app": "autobrain-backup"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/login":
            return self._login()
        if path == "/api/config":
            if not self._authorized():
                return self._unauthorized()
            return self._save_config()
        if path == "/api/backup/run":
            if not self._authorized():
                return self._unauthorized()
            return self._send(200, self.app.run_backup_now())
        if path == "/api/backup/restore":
            if not self._authorized():
                return self._unauthorized()
            return self._restore()
        if path == "/ingest":
            return self._ingest()
        self._send(404, {"error": "not found"})

    # --- handlers ---
    def _login(self):
        try:
            payload = self._read_json()
        except (json.JSONDecodeError, ValueError):
            return self._send(400, {"error": "invalid JSON"})
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        cfg = self.app.config.get()
        u = cfg.get("gui_user") or ""
        p = cfg.get("gui_password") or ""
        if u and hmac.compare_digest(username, u) and hmac.compare_digest(password, p):
            token = self.app.new_session()
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", f"autobrain_session={token}; HttpOnly; Path=/; SameSite=Lax")
            self.end_headers()
            self.wfile.write(body)
            return
        return self._send(401, {"error": "invalid username or password"})

    def _save_config(self):
        try:
            payload = self._read_json()
        except (json.JSONDecodeError, ValueError):
            return self._send(400, {"error": "invalid JSON"})
        if not isinstance(payload, dict):
            return self._send(400, {"error": "invalid config"})
        # preserve masked values: an all-asterisk field means "keep current"
        current = self.app.config.get()
        for k in ("api_key", "gui_key", "gui_password", "ingest_key"):
            if payload.get(k) == "********":
                payload[k] = current.get(k)
        email = payload.get("email")
        if isinstance(email, dict) and email.get("smtp_password") == "********":
            email["smtp_password"] = current.get("email", {}).get("smtp_password")
        self.app.config.save(payload)
        self.app.engine.backup_dir = Path(self.app.config.get().get("backup_dir"))
        return self._send(200, {"ok": True, "config": self.app.config.masked()})

    def _download(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = (qs.get("name") or [""])[0]
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return self._send(400, {"error": "invalid backup name"})
        try:
            path = self.app.engine._resolve_source(name)
        except BackupError as e:
            return self._send(404, {"error": str(e)})
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _restore(self):
        try:
            payload = self._read_json()
        except (json.JSONDecodeError, ValueError):
            return self._send(400, {"error": "invalid JSON"})
        confirm = bool(payload.get("confirm"))
        if not confirm:
            return self._send(400, {"error": "confirm=true is required (restore wipes existing data)"})
        try:
            status, name = self.app.engine.restore(payload, confirm=True)
        except BackupError as e:
            return self._send(400, {"error": str(e)})
        self.app.engine.alert_restore(status, name)
        return self._send(200, {"ok": True, "restored": name, "http": status})

    def _ingest(self):
        """Receives a backup pushed by the autobrain-backup-agent."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 512 * 1024 * 1024:
            return self._send(400, {"error": "bad content length"})
        body = self.rfile.read(length)
        key = self.headers.get("X-Ingest-Key", "")
        try:
            name = self.app.engine.ingest(body, key)
        except BackupError as e:
            return self._send(400, {"error": str(e)})
        return self._send(200, {"ok": True, "saved": name})

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/backup/delete" and self._authorized():
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (qs.get("name") or [""])[0]
            if not name or "/" in name or "\\" in name or name.startswith("."):
                return self._send(400, {"error": "invalid backup name"})
            for d in (self.app.engine._hourly_dir(), self.app.engine._daily_dir(),
                      self.app.engine._weekly_dir()):
                p = d / name
                if p.exists():
                    p.unlink()
                    return self._send(200, {"ok": True, "deleted": name})
            return self._send(404, {"error": "backup not found"})
        self._send(404, {"error": "not found"})


class BackupServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, app):
        self.app = app
        super().__init__(addr, Handler)


def main(argv=None):
    p = argparse.ArgumentParser(description="autobrain-backup web service")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--config", default="/config/autobrain-backup.json")
    p.add_argument("--backups", default=None, help="override backup data directory")
    args = p.parse_args(argv)

    app = App(args.config, args.backups)
    try:
        srv = BackupServer((args.host, args.port), app)
        print(f"autobrain-backup {VERSION} listening on {args.host}:{args.port}", flush=True)
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()


if __name__ == "__main__":
    sys.exit(main())
