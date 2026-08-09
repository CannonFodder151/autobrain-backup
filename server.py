#!/usr/bin/env python3
"""autobrain-backup web service.

Serves the backup GUI + JSON API over stdlib http.server, runs per-instance
schedulers in a background thread, and accepts pushed backups from the
autobrain-backup-agent via POST /ingest.

Multi-tenant: the config holds a list of AutoBrain instances; the main page
lists them, each with a nickname. Config lives in a file on the docker host
(mounted folder), editable in the GUI at /config. Per-instance run state
persists next to each instance's backups.
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

from engine import BackupEngine, BackupError, Config, Mailer, State

STATIC_DIR = Path(__file__).resolve().parent / "static"
VERSION = "2.0.0"

SESSION_TTL = 24 * 3600


class App:
    def __init__(self, config_path, backup_dir):
        self.config = Config(config_path)
        self._engines = {}
        self._sessions = {}
        self._backup_dir_override = backup_dir
        self._migrate_legacy_state()
        self._sched_stop = threading.Event()
        self._sched = threading.Thread(target=self._scheduler, daemon=True)
        self._sched.start()

    # --- engine / paths ---
    def _instance_backup_dir(self, inst):
        if self._backup_dir_override:
            return Path(self._backup_dir_override) / inst["id"]
        explicit = (inst.get("backup_dir") or "").strip()
        if explicit:
            return Path(explicit)
        return Path(self.config.get().get("backup_dir") or "/backups") / inst["id"]

    def _engine(self, iid):
        inst = self.config.get_instance(iid)
        if not inst:
            raise BackupError(f"instance not found: {iid}")
        eng = self._engines.get(iid)
        if eng is None:
            eng = self._engines[iid] = BackupEngine(
                inst, State(self._instance_backup_dir(inst) / "state.json"),
                self._instance_backup_dir(inst), self.config.get().get("email") or {})
        else:
            eng.instance = inst
            eng.backup_dir = self._instance_backup_dir(inst)
            eng.email_cfg = self.config.get().get("email") or {}
        return eng

    def _migrate_legacy_state(self):
        """v1 kept a single state file next to the config; move it into the
        migrated instance's backup folder on first run."""
        legacy = Path(self.config.path).with_suffix(".state.json")
        if not legacy.exists():
            return
        for inst in self.config.instances():
            target = self._instance_backup_dir(inst) / "state.json"
            if not target.exists():
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    legacy.replace(target)
                except OSError:
                    return
            break
        try:
            legacy.unlink()
        except OSError:
            pass

    def _mailer(self):
        return Mailer(self.config.get())

    def instances_list(self):
        out = []
        for inst in self.config.instances():
            try:
                st = self._engine(inst["id"]).status()
            except Exception:
                st = {}
            out.append({
                "id": inst["id"],
                "nickname": inst.get("nickname") or "",
                "instance_url": inst.get("instance_url") or None,
                "enabled": bool(inst.get("enabled", True)),
                "last_status": st.get("last_status"),
                "last_backup_at": st.get("last_backup_at"),
                "last_error": st.get("last_error"),
                "backups": st.get("backups"),
            })
        return out

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
                for inst in self.config.instances():
                    try:
                        self._scheduler_step(inst)
                    except Exception:
                        pass
            except Exception:
                pass
            self._sched_stop.wait(60)

    def _scheduler_step(self, inst):
        if not inst.get("enabled", True):
            return
        eng = self._engine(inst["id"])
        interval = max(60, int(inst.get("schedule_interval", 3600)))
        st = eng.state.get()
        last = None
        if st.get("last_run"):
            try:
                last = datetime.fromisoformat(st["last_run"])
            except ValueError:
                last = None
        now = datetime.now(timezone.utc)
        due = last is None or (now - last).total_seconds() >= interval
        if last is None:
            next_run = now + interval
        else:
            next_run = last + interval
        eng.state.update(next_run_at=next_run.isoformat())
        if due and not inst.get("instance_url"):
            return
        if due:
            try:
                eng.run_backup()
            except BackupError as e:
                eng.state.update(last_status="fail", last_error=str(e), last_run=_utcnow_iso())
                eng.state.touch_counters(ok=False)
                eng.alert_failure(e)
            except Exception as e:  # defensive: never kill the scheduler
                eng.state.update(last_status="fail", last_error=f"unexpected: {e}", last_run=_utcnow_iso())
                eng.state.touch_counters(ok=False)

    def run_backup_now(self, iid):
        try:
            name = self._engine(iid).run_backup()
            return {"ok": True, "name": name}
        except BackupError as e:
            eng = self._engine(iid)
            eng.state.update(last_status="fail", last_error=str(e), last_run=_utcnow_iso())
            eng.state.touch_counters(ok=False)
            eng.alert_failure(e)
            eng.alert_corruption(e) if "corrupt" in str(e).lower() else None
            return {"ok": False, "error": str(e)}

    def test_email(self):
        ok, err = self._mailer().test()
        return {"ok": bool(ok), "error": err}

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

    def _instance_id(self):
        """Query 'instance' param; fall back to the sole instance when there is
        exactly one (keeps single-instance clients working)."""
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        iid = (qs.get("instance") or [""])[0]
        if iid:
            return iid
        insts = self.app.config.instances()
        if len(insts) == 1:
            return insts[0]["id"]
        return None

    def _need_instance(self):
        iid = self._instance_id()
        if not iid:
            self._send(400, {"error": "instance required (specify ?instance=id)"})
            return None
        return iid

    def _engine(self, iid):
        try:
            return self.app._engine(iid)
        except BackupError as e:
            self._send(404, {"error": str(e)})
            return None

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
        if path == "/api/instances":
            if not self._authorized():
                return self._unauthorized()
            return self._send(200, {"instances": self.app.instances_list()})
        if path == "/api/config":
            if not self._authorized():
                return self._unauthorized()
            return self._config_view()
        if path == "/api/status":
            if not self._authorized():
                return self._unauthorized()
            iid = self._need_instance()
            if iid is None:
                return
            eng = self._engine(iid)
            if eng is None:
                return
            return self._send(200, eng.status())
        if path == "/api/backups":
            if not self._authorized():
                return self._unauthorized()
            iid = self._need_instance()
            if iid is None:
                return
            eng = self._engine(iid)
            if eng is None:
                return
            return self._send(200, eng.list_backups())
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
        if path == "/api/instances":
            if not self._authorized():
                return self._unauthorized()
            return self._create_instance()
        if path == "/api/instances/update":
            if not self._authorized():
                return self._unauthorized()
            return self._update_instance()
        if path == "/api/config":
            if not self._authorized():
                return self._unauthorized()
            return self._save_config()
        if path == "/api/email/test":
            if not self._authorized():
                return self._unauthorized()
            return self._send(200, self.app.test_email())
        if path == "/api/backup/run":
            if not self._authorized():
                return self._unauthorized()
            iid = self._need_instance()
            if iid is None:
                return
            if self._engine(iid) is None:
                return
            return self._send(200, self.app.run_backup_now(iid))
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

    def _config_view(self):
        cfg = self.app.config.masked()
        iid = self._instance_id()
        out = {"instances": cfg.get("instances") or [],
               "instance": None, "gui_key": cfg.get("gui_key"),
               "gui_user": cfg.get("gui_user"), "gui_password": cfg.get("gui_password"),
               "backup_dir": cfg.get("backup_dir"), "email": cfg.get("email")}
        if iid:
            for inst in out["instances"]:
                if inst.get("id") == iid:
                    out["instance"] = inst
                    break
        return self._send(200, out)

    def _create_instance(self):
        try:
            payload = self._read_json()
        except (json.JSONDecodeError, ValueError):
            return self._send(400, {"error": "invalid JSON"})
        if not isinstance(payload, dict):
            return self._send(400, {"error": "invalid instance"})
        for k in ("api_key", "ingest_key"):
            if payload.get(k) == "********":
                payload[k] = ""
        inst = self.app.config.add_instance(payload)
        masked = self.app.config.masked()
        for m in masked.get("instances") or []:
            if m.get("id") == inst["id"]:
                inst = m
                break
        return self._send(200, {"ok": True, "instance": inst})

    def _update_instance(self):
        try:
            payload = self._read_json()
        except (json.JSONDecodeError, ValueError):
            return self._send(400, {"error": "invalid JSON"})
        if not isinstance(payload, dict):
            return self._send(400, {"error": "invalid instance"})
        iid = str(payload.get("id") or "")
        if not iid:
            iid = self._instance_id()
        if not iid:
            return self._send(400, {"error": "instance id required"})
        current = self.app.config.get_instance(iid)
        if not current:
            return self._send(404, {"error": "instance not found"})
        for k in ("api_key", "ingest_key"):
            if payload.get(k) == "********":
                payload[k] = current.get(k)
        updated = self.app.config.update_instance(iid, payload)
        return self._send(200, {"ok": True, "instance": self._mask_inst(updated)})

    def _mask_inst(self, inst):
        cfg = self.app.config.masked()
        for m in cfg.get("instances") or []:
            if m.get("id") == inst["id"]:
                return m
        return inst

    def _save_config(self):
        try:
            payload = self._read_json()
        except (json.JSONDecodeError, ValueError):
            return self._send(400, {"error": "invalid JSON"})
        if not isinstance(payload, dict):
            return self._send(400, {"error": "invalid config"})
        current = self.app.config.get()
        iid = str(payload.pop("instance_id", "") or "") or self._instance_id()
        inst_fields = {}
        for k in ("nickname", "instance_url", "api_key", "backup_endpoint",
                  "restore_endpoint", "schedule_interval", "ingest_key",
                  "retention", "enabled"):
            if k in payload:
                inst_fields[k] = payload.pop(k)
        if inst_fields:
            if not iid:
                return self._send(400, {"error": "instance required (specify ?instance=id)"})
            cur = self.app.config.get_instance(iid)
            if not cur:
                return self._send(404, {"error": "instance not found"})
            for k in ("api_key", "ingest_key"):
                if inst_fields.get(k) == "********":
                    inst_fields[k] = cur.get(k)
            self.app.config.update_instance(iid, inst_fields)
        save = {}
        if "email" in payload:
            email = payload["email"]
            if isinstance(email, dict) and email.get("smtp_password") == "********":
                email["smtp_password"] = current.get("email", {}).get("smtp_password")
            save["email"] = email
        for k in ("gui_key", "gui_user", "gui_password", "backup_dir"):
            if k in payload:
                if payload.get(k) == "********":
                    payload[k] = current.get(k)
                save[k] = payload[k]
        if save:
            self.app.config.save(save)
        cfg = self.app.config.masked()
        out = {"ok": True, "config": cfg, "instances": cfg.get("instances") or []}
        if iid:
            for inst in cfg.get("instances") or []:
                if inst.get("id") == iid:
                    out["instance"] = inst
                    break
        return self._send(200, out)

    def _download(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = (qs.get("name") or [""])[0]
        iid = self._instance_id()
        if not iid:
            return self._send(400, {"error": "instance required"})
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return self._send(400, {"error": "invalid backup name"})
        eng = self._engine(iid)
        if eng is None:
            return
        try:
            path = eng._resolve_source(name)
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
        iid = self._need_instance()
        if iid is None:
            return
        try:
            payload = self._read_json()
        except (json.JSONDecodeError, ValueError):
            return self._send(400, {"error": "invalid JSON"})
        confirm = bool(payload.get("confirm"))
        if not confirm:
            return self._send(400, {"error": "confirm=true is required (restore wipes existing data)"})
        eng = self._engine(iid)
        if eng is None:
            return
        try:
            status, name = eng.restore(payload, confirm=True)
        except BackupError as e:
            return self._send(400, {"error": str(e)})
        eng.alert_restore(status, name)
        return self._send(200, {"ok": True, "restored": name, "http": status})

    def _ingest(self):
        """Receives a backup pushed by the autobrain-backup-agent."""
        iid = self._need_instance()
        if iid is None:
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 512 * 1024 * 1024:
            return self._send(400, {"error": "bad content length"})
        body = self.rfile.read(length)
        key = self.headers.get("X-Ingest-Key", "")
        eng = self._engine(iid)
        if eng is None:
            return
        try:
            name = eng.ingest(body, key)
        except BackupError as e:
            return self._send(400, {"error": str(e)})
        return self._send(200, {"ok": True, "saved": name})

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/instances" and self._authorized():
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            iid = (qs.get("instance") or [""])[0]
            if not iid:
                return self._send(400, {"error": "instance required"})
            self.app._engines.pop(iid, None)
            if not self.app.config.remove_instance(iid):
                return self._send(404, {"error": "instance not found"})
            return self._send(200, {"ok": True, "deleted": iid})
        if path == "/api/backup/delete" and self._authorized():
            iid = self._need_instance()
            if iid is None:
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (qs.get("name") or [""])[0]
            if not name or "/" in name or "\\" in name or name.startswith("."):
                return self._send(400, {"error": "invalid backup name"})
            eng = self._engine(iid)
            if eng is None:
                return
            for d in (eng._hourly_dir(), eng._daily_dir(), eng._weekly_dir()):
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
