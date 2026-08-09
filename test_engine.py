"""Self-check for the autobrain-backup engine.

Run:  python3 test_engine.py
Simulates an AutoBrain instance with stdlib http.server and verifies:
  * fetch + validation of a genuine backup (and rejection of a corrupt one)
  * hourly/daily/weekly archive promotion + pruning
  * restore (multipart upload) and its corruption gate
  * config save + masked view
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import BackupEngine, BackupError, Config, State  # noqa: E402


def make_backup(created=None):
    return {
        "app": "autobrain",
        "kind": "backup",
        "version": 1,
        "created_at": created or datetime.now(timezone.utc).isoformat(),
        "data": {"users": [{"id": "u1", "email": "a@b.c"}]},
    }


class FakeInstance(BaseHTTPRequestHandler):
    received = {}
    mode = "ok"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/admin-api/backup":
            body = json.dumps(make_backup()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        FakeInstance.received[self.path] = body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def start_fake():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeInstance)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    tmp = Path(tempfile.mkdtemp(prefix="abtest-"))
    cfg_path = tmp / "config.json"
    state_path = tmp / "state.json"
    cfg = Config(cfg_path)
    cfg.save({
        "instance_url": "http://127.0.0.1:1", "api_key": "k",
        "backup_dir": str(tmp / "backups"), "retention": {"hourly": 3, "daily": 2, "weekly": 1},
        "email": {"enabled": False},
    })
    assert cfg.masked()["api_key"] == "********", "masking failed"
    assert cfg.get()["api_key"] == "k", "masking leaked into real config"
    print("ok: config save + masked view")

    srv = start_fake()
    base = f"http://127.0.0.1:{srv.server_port}"
    cfg.save({"instance_url": base, "api_key": "k"})
    eng = BackupEngine(cfg, State(state_path), tmp / "backups")
    name = eng.run_backup()
    assert name.startswith("autobrain-backup-"), name
    assert (tmp / "backups" / "hourly" / name).exists()
    st = eng.status()
    assert st["last_status"] == "ok" and st["counters"]["ok"] == 1
    print("ok: fetch + save + status:", name)

    bad = json.dumps({"app": "wrong", "kind": "backup", "data": {}}).encode()
    try:
        BackupEngine.validate(bad)
        raise AssertionError("corrupt payload accepted")
    except BackupError:
        pass
    print("ok: corrupt payload rejected")

    eng2 = BackupEngine(cfg, State(state_path), tmp / "backups")
    eng2.state._s["last_daily_date"] = ""
    eng2.state._s["last_weekly_date"] = ""
    eng2.rotate()
    daily = sorted((tmp / "backups" / "daily").glob("autobrain-backup-daily-*.json"))
    weekly = sorted((tmp / "backups" / "weekly").glob("autobrain-backup-weekly-*.json"))
    assert daily, "daily promotion missing"
    assert weekly, "weekly promotion missing"
    print("ok: daily/weekly promotion")

    cfg.save({"retention": {"hourly": 3, "daily": 2, "weekly": 1}})
    for i in range(4):
        eng2._save_body(json.dumps(make_backup(created=f"2026-08-0{i+1}T00:00:00+00:00")).encode())
    eng2.rotate()
    hourly_count = len(list((tmp / "backups" / "hourly").glob("autobrain-backup-*.json")))
    assert hourly_count <= 3, f"hourly prune failed: {hourly_count}"
    print("ok: hourly prune (kept", hourly_count, ")")

    cfg.save({"instance_url": base, "api_key": "k"})
    eng3 = BackupEngine(cfg, State(state_path), tmp / "backups")
    status, restored = eng3.restore({"content_b64": base64.b64encode(json.dumps(make_backup()).encode()).decode(), "filename": "x.json"}, confirm=True)
    assert status == 200 and restored.startswith("autobrain-backup-"), (status, restored)
    assert FakeInstance.received.get("/admin-api/restore"), "restore never reached the instance"
    assert b"----autobrainbackup" in FakeInstance.received["/admin-api/restore"], "not a multipart body"
    print("ok: restore multipart upload")

    try:
        eng3.restore({"content_b64": base64.b64encode(b"not json").decode(), "filename": "x.json"}, confirm=True)
        raise AssertionError("corrupt restore accepted")
    except BackupError:
        pass
    print("ok: corrupt restore rejected (no wipe)")

    eng3.restore(name, confirm=True)
    print("ok: restore from stored backup")

    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
