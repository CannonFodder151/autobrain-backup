"""Self-check for the autobrain-backup engine (multi-tenant).

Run:  python3 test_engine.py
Simulates an AutoBrain instance with stdlib http.server and verifies:
  * v1 -> v2 config migration (single instance wrapped)
  * instance CRUD (create / rename nickname / delete) + masked view
  * multi-instance backup (two instances, separate backup folders + state)
  * fetch + validation of a genuine backup (and rejection of a corrupt one)
  * fetch + validation of the MinIO image archive (same stamp)
  * hourly/daily/weekly archive promotion + pruning (DB + images)
  * restore (multipart upload) and its corruption gate (DB + images)
  * test-email success (fake SMTP) and failure (unreachable host)
  * HTTP API: login, instances list, create/save, single-instance fallback
"""

import base64
import http.client
import io
import json
import shutil
import socket
import socketserver
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import BackupEngine, BackupError, Config, Mailer, State  # noqa: E402
import server  # noqa: E402


def make_backup(created=None):
    return {
        "app": "autobrain",
        "kind": "backup",
        "version": 1,
        "created_at": created or datetime.now(timezone.utc).isoformat(),
        "data": {"users": [{"id": "u1", "email": "a@b.c"}]},
    }


def make_assets():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"\xff\xd8\xff fake-image"
        info = tarfile.TarInfo("vehicles/v1/photo.jpg")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeInstance(BaseHTTPRequestHandler):
    received = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/api/v1/admin-api/backup":
            body = json.dumps(make_backup()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/v1/admin-api/assets/backup":
            body = make_assets()
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
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


class FakeSMTP(socketserver.StreamRequestHandler):
    """Minimal SMTP server that accepts a message and records it."""

    inbox = []

    def handle(self):
        self.wfile.write(b"220 fake ESMTP\r\n")
        data_mode = False
        msg = []
        while True:
            line = self.rfile.readline()
            if not line:
                break
            if data_mode:
                if line in (b".\r\n", b".\n"):
                    FakeSMTP.inbox.append(b"".join(msg))
                    self.wfile.write(b"250 ok\r\n")
                    data_mode = False
                else:
                    msg.append(line)
                continue
            cmd = line.strip().upper()
            if cmd.startswith(b"EHLO"):
                self.wfile.write(b"250-fake\r\n250 AUTH PLAIN\r\n")
            elif cmd.startswith(b"HELO"):
                self.wfile.write(b"250 fake\r\n")
            elif cmd.startswith(b"AUTH"):
                self.wfile.write(b"235 ok\r\n")
            elif cmd.startswith(b"MAIL") or cmd.startswith(b"RCPT"):
                self.wfile.write(b"250 ok\r\n")
            elif cmd.startswith(b"DATA"):
                self.wfile.write(b"354 go ahead\r\n")
                data_mode = True
                msg = []
            elif cmd.startswith(b"QUIT"):
                self.wfile.write(b"221 bye\r\n")
                break
            elif cmd.startswith(b"RSET") or cmd.startswith(b"NOOP"):
                self.wfile.write(b"250 ok\r\n")
            else:
                self.wfile.write(b"250 ok\r\n")


def start_smtp():
    FakeSMTP.inbox = []
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeSMTP)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def engine_for(cfg, iid, tmp, base_url, key="k"):
    inst = cfg.get_instance(iid)
    bdir = tmp / "backups" / iid
    return BackupEngine(inst, State(bdir / "state.json"), bdir, cfg.get().get("email") or {})


def test_migration(tmp):
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps({
        "instance_url": "http://old.example", "api_key": "k1",
        "backup_dir": "/backups", "schedule_interval": 3600,
        "retention": {"hourly": 24, "daily": 30, "weekly": 12},
        "email": {"enabled": False},
    }))
    cfg = Config(cfg_path)
    insts = cfg.instances()
    assert len(insts) == 1 and insts[0]["instance_url"] == "http://old.example", insts
    assert insts[0]["id"] == "inst_default"
    assert cfg.masked()["instances"][0]["api_key"] == "********"
    assert cfg.get()["instances"][0]["api_key"] == "k1"
    print("ok: v1 config migrated to single instance + masked view")


def test_instance_crud(cfg):
    a = cfg.add_instance({"nickname": "Hosted", "instance_url": "https://a.example",
                          "api_key": "ka", "schedule_interval": 3600})
    assert a["nickname"] == "Hosted" and a["api_key"] == "ka"
    renamed = cfg.update_instance(a["id"], {"nickname": "Hosted prod"})
    assert renamed["nickname"] == "Hosted prod"
    assert cfg.get_instance(a["id"])["api_key"] == "ka"
    assert cfg.remove_instance(a["id"]) is True
    assert cfg.get_instance(a["id"]) is None
    print("ok: instance create / rename / delete")


def test_multi_instance(tmp, base_url):
    cfg_path = tmp / "config.json"
    cfg = Config(cfg_path)
    a = cfg.add_instance({"nickname": "One", "instance_url": base_url, "api_key": "k",
                          "schedule_interval": 3600})
    b = cfg.add_instance({"nickname": "Two", "instance_url": base_url, "api_key": "k",
                          "schedule_interval": 3600})
    ea = engine_for(cfg, a["id"], tmp, base_url)
    eb = engine_for(cfg, b["id"], tmp, base_url)
    na = ea.run_backup()
    nb = eb.run_backup()
    assert na.startswith("autobrain-backup-") and nb.startswith("autobrain-backup-")
    assert (tmp / "backups" / a["id"] / "hourly" / na).exists()
    assert (tmp / "backups" / b["id"] / "hourly" / nb).exists()
    sa, sb = ea.status(), eb.status()
    assert sa["nickname"] == "One" and sb["nickname"] == "Two"
    assert sa["counters"]["ok"] == 1 and sb["counters"]["ok"] == 1
    assert sa["backup_dir"] != sb["backup_dir"]
    assert sa["assets"]["counts"]["hourly"] == 1 and sa["assets"]["last_assets_error"] is None
    assert list((tmp / "backups" / a["id"] / "hourly" / "images").glob("autobrain-assets-*.tar.gz")), "image archive missing"
    print("ok: two instances back up into separate folders with separate state (db + images)")


def test_validation():
    bad = json.dumps({"app": "wrong", "kind": "backup", "data": {}}).encode()
    try:
        BackupEngine.validate(bad)
        raise AssertionError("corrupt payload accepted")
    except BackupError:
        pass
    print("ok: corrupt payload rejected")


def test_retention(tmp, base_url, cfg):
    a = cfg.get_instance("inst_default")
    if a is None:
        a = cfg.add_instance({"nickname": "ret", "instance_url": base_url, "api_key": "k"})
    bdir = tmp / "backups" / a["id"]
    eng = engine_for(cfg, a["id"], tmp, base_url)
    eng.run_backup()
    eng.state._s["last_daily_date"] = ""
    eng.state._s["last_weekly_date"] = ""
    eng.rotate()
    daily = sorted((bdir / "daily").glob("autobrain-backup-daily-*.json"))
    weekly = sorted((bdir / "weekly").glob("autobrain-backup-weekly-*.json"))
    assert daily, "daily promotion missing"
    assert weekly, "weekly promotion missing"
    cfg.update_instance(a["id"], {"retention": {"hourly": 3, "daily": 2, "weekly": 1}})
    eng.instance = cfg.get_instance(a["id"])
    for i in range(4):
        eng._save_body(json.dumps(make_backup(created=f"2026-08-0{i+1}T00:00:00+00:00")).encode())
        eng._save_assets(make_assets(), f"2026-08-0{i+1}T000000")
    eng.rotate()
    hourly_count = len(list((bdir / "hourly").glob("autobrain-backup-*.json")))
    hourly_img = len(list((bdir / "hourly" / "images").glob("autobrain-assets-*.tar.gz")))
    assert hourly_count <= 3, f"hourly prune failed: {hourly_count}"
    assert hourly_img <= 3, f"hourly image prune failed: {hourly_img}"
    daily_img = list((bdir / "daily" / "images").glob("autobrain-assets-daily-*.tar.gz"))
    weekly_img = list((bdir / "weekly" / "images").glob("autobrain-assets-weekly-*.tar.gz"))
    assert daily_img and weekly_img, "image promotion missing"
    print("ok: daily/weekly promotion + prune (db kept", hourly_count, ", images kept", hourly_img, ")")


def test_restore(tmp, base_url, cfg):
    FakeInstance.received = {}
    a = cfg.get_instance("inst_default") or cfg.add_instance({"instance_url": base_url, "api_key": "k"})
    eng = engine_for(cfg, a["id"], tmp, base_url)
    name = eng.run_backup()
    status, restored = eng.restore({"content_b64": base64.b64encode(json.dumps(make_backup()).encode()).decode(), "filename": "x.json"}, confirm=True)
    assert status == 200 and restored.startswith("autobrain-backup-"), (status, restored)
    assert FakeInstance.received.get("/api/v1/admin-api/restore"), "restore never reached the instance"
    assert b"----autobrainbackup" in FakeInstance.received["/api/v1/admin-api/restore"], "not a multipart body"
    try:
        eng.restore({"content_b64": base64.b64encode(b"not json").decode(), "filename": "x.json"}, confirm=True)
        raise AssertionError("corrupt restore accepted")
    except BackupError:
        pass
    eng.restore(name, confirm=True)
    print("ok: restore multipart upload + corruption gate")


def test_assets_roundtrip(tmp, base_url, cfg):
    FakeInstance.received = {}
    a = cfg.get_instance("inst_default") or cfg.add_instance({"instance_url": base_url, "api_key": "k"})
    eng = engine_for(cfg, a["id"], tmp, base_url)
    name = eng.run_backup()
    stamp = name[len("autobrain-backup-"):-len(".json")]
    img_name = f"autobrain-assets-{stamp}.tar.gz"
    img_path = tmp / "backups" / a["id"] / "hourly" / "images" / img_name
    assert img_path.exists(), "image archive not saved alongside DB snapshot"

    try:
        BackupEngine.validate_assets(b"not a tar.gz")
        raise AssertionError("corrupt image archive accepted")
    except BackupError:
        pass

    status, restored = eng.restore_assets({"content_b64": base64.b64encode(make_assets()).decode(), "filename": "x.tar.gz"}, confirm=True)
    assert status == 200 and restored.startswith("autobrain-assets-"), (status, restored)
    assert FakeInstance.received.get("/api/v1/admin-api/assets/restore"), "image restore never reached the instance"
    assert b"----autobrainbackup" in FakeInstance.received["/api/v1/admin-api/assets/restore"], "not a multipart body"

    try:
        eng.restore_assets({"content_b64": base64.b64encode(b"corrupt").decode(), "filename": "x.tar.gz"}, confirm=True)
        raise AssertionError("corrupt image restore accepted")
    except BackupError:
        pass

    try:
        eng.restore_assets(img_name, confirm=False)
        raise AssertionError("image restore without confirm accepted")
    except BackupError:
        pass

    status, restored = eng.restore_assets(img_name, confirm=True)
    assert status == 200 and restored == img_name, (status, restored)
    assert FakeInstance.received.get("/api/v1/admin-api/assets/restore"), "stored image restore never reached the instance"

    listing = eng.list_backups()
    assert listing["images"]["hourly"] and listing["images"]["hourly"][0]["name"] == img_name
    print("ok: image archive fetch/save/rotate/restore round-trip")


def test_api_assets(tmp, base_url):
    import server

    cfg_path = tmp / "apicfg.json"
    app = server.App(str(cfg_path), tmp / "backups")
    inst = app.config.add_instance({"nickname": "T", "instance_url": base_url, "api_key": "k"})
    iid = inst["id"]
    srv = server.BackupServer(("127.0.0.1", 0), app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        app._engine(iid).run_backup()
        with urllib.request.urlopen(base + f"/api/backups?instance={iid}", timeout=5) as r:
            d = json.loads(r.read())
        img = d["images"]["hourly"][0]["name"]
        dbname = d["hourly"][0]["name"]
        with urllib.request.urlopen(base + f"/api/assets/download?instance={iid}&name={img}", timeout=5) as r:
            assert r.status == 200 and r.read()[:2] == b"\x1f\x8b"
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        payload = json.dumps({"name": img, "confirm": False})
        c.request("POST", f"/api/assets/restore?instance={iid}", body=payload, headers={"Content-Type": "application/json"})
        r = c.getresponse(); r.read()
        assert r.status == 400, "restore without confirm accepted"
        payload = json.dumps({"name": img, "confirm": True})
        c.request("POST", f"/api/assets/restore?instance={iid}", body=payload, headers={"Content-Type": "application/json"})
        r = c.getresponse(); d = json.loads(r.read() or b"{}")
        assert r.status == 200 and d["ok"] is True, (r.status, d)
        assert FakeInstance.received.get("/api/v1/admin-api/assets/restore"), "restore never reached the instance"
        c.request("POST", f"/api/backup/restore?instance={iid}", body=json.dumps({"name": dbname, "confirm": True}), headers={"Content-Type": "application/json"})
        r = c.getresponse(); d = json.loads(r.read() or b"{}")
        assert r.status == 200 and d["ok"] is True, (r.status, d)
        print("ok: HTTP assets download + confirm-gated restore (and stored DB restore)")
    finally:
        srv.shutdown()
        app.stop()


def test_email(tmp):
    srv = start_smtp()
    cfg = {"email": {"enabled": True, "smtp_host": "127.0.0.1", "smtp_port": srv.server_address[1],
                     "smtp_user": "", "use_tls": False, "from": "noreply@ab.app", "to": ["ops@ab.app"]}}
    ok, err = Mailer(cfg).test()
    assert ok is True, err
    assert b"test email" in FakeSMTP.inbox[0].lower()
    srv.shutdown()
    print("ok: test email delivered via SMTP")

    dead = {"email": {"enabled": True, "smtp_host": "127.0.0.1", "smtp_port": 1,
                      "use_tls": False, "from": "noreply@ab.app", "to": ["ops@ab.app"]}}
    ok, err = Mailer(dead).test()
    assert ok is False and err, err
    print("ok: test email failure reported:", err[:40])

    disabled = {"email": {"enabled": False}}
    ok, err = Mailer(disabled).test()
    assert ok is False
    print("ok: test email reports when alerts disabled")


def _post(c, path, body, cookie=None):
    c.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})})
    r = c.getresponse(); return r, json.loads(r.read() or b"{}")


def test_api(tmp):
    import server

    cfg_path = tmp / "cfg.json"
    cfg_path.write_text(json.dumps({"gui_user": "admin", "gui_password": "pw", "gui_key": "legacykey",
                                    "email": {"enabled": False}}))
    app = server.App(cfg_path, tmp / "backups")
    srv = server.BackupServer(("127.0.0.1", 0), app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        r = urllib.request.urlopen(base + "/", timeout=5)
        assert r.status == 200 and b"Instances" in r.read()
        try:
            urllib.request.urlopen(base + "/api/instances", timeout=5)
            raise AssertionError("instances open without login")
        except urllib.error.HTTPError as e:
            assert e.code == 401
        print("ok: / serves console; api requires login")

        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        r, _ = _post(c, "/api/login", {"username": "admin", "password": "bad"})
        assert r.status == 401
        r, _ = _post(c, "/api/login", {"username": "admin", "password": "pw"})
        assert r.status == 200
        cookie = r.getheader("Set-Cookie").split(";")[0]
        print("ok: login + session cookie")

        c.request("GET", "/api/instances", headers={"Cookie": cookie})
        r = c.getresponse(); d = json.loads(r.read())
        assert r.status == 200 and d["instances"] == []
        print("ok: empty instances list")

        r, d = _post(c, "/api/instances", {"nickname": "One", "instance_url": "http://127.0.0.1:1", "api_key": "k"}, cookie)
        assert r.status == 200 and d["instance"]["nickname"] == "One"
        iid = d["instance"]["id"]
        assert d["instance"]["api_key"] == "********"
        print("ok: create instance via API (masked in response)")

        c.request("GET", "/api/status", headers={"Cookie": cookie})
        r = c.getresponse()
        assert r.status == 200, "single-instance fallback failed"
        c.request("GET", "/api/status?instance=nope", headers={"Cookie": cookie})
        r = c.getresponse()
        assert r.status == 404, r.status
        print("ok: status resolves sole instance; bad id rejected")

        r, d = _post(c, "/api/instances", {"nickname": "Two", "instance_url": "http://127.0.0.1:2", "api_key": "k2"}, cookie)
        assert r.status == 200
        print("ok: second instance added")

        c.request("GET", "/api/status", headers={"Cookie": cookie})
        r = c.getresponse()
        assert r.status == 400, "ambiguous instance must 400"
        print("ok: ambiguous instance requires ?instance=")

        r, d = _post(c, "/api/instances/update", {"id": iid, "nickname": "Renamed"}, cookie)
        assert r.status == 200 and d["instance"]["nickname"] == "Renamed"
        print("ok: rename via API")

        c.request("DELETE", "/api/instances?instance=" + iid, headers={"Cookie": cookie})
        r = c.getresponse()
        assert r.status == 200
        c.request("GET", "/api/instances", headers={"Cookie": cookie})
        r = c.getresponse(); d = json.loads(r.read())
        assert len(d["instances"]) == 1
        print("ok: delete via API")
    finally:
        srv.shutdown()
        app.stop()


def test_scheduler_due(tmp):
    cfg_path = tmp / "sched.json"
    cfg_path.write_text(json.dumps({
        "backup_dir": "/backups",
        "email": {"enabled": False},
        "instances": [{
            "id": "inst_default", "nickname": "sched",
            "instance_url": "", "api_key": "k",
            "schedule_interval": 3600, "enabled": True,
        }],
    }))
    data_dir = tmp / "sched-data"
    state_dir = data_dir / "inst_default"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps({
        "last_run": "2020-01-01T00:00:00+00:00",
    }))
    app = server.App(str(cfg_path), str(data_dir))
    try:
        inst = app.config.instances()[0]
        app._scheduler_step(inst)
        st = app._engine("inst_default").state.get()
        assert st.get("next_run_at"), st
        assert st["next_run_at"].startswith("2020-01-01T01:00:00"), st
    finally:
        app.stop()
    print("ok: scheduler computes next_run_at from last_run (regression: datetime + int)")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="abtest-"))
    try:
        test_migration(tmp)
        test_instance_crud(Config(tmp / "crud.json"))
        test_scheduler_due(tmp)
        srv = start_fake()
        base_url = f"http://127.0.0.1:{srv.server_port}"
        test_multi_instance(tmp, base_url)
        test_validation()
        cfg = Config(tmp / "retention.json")
        cfg.add_instance({"id": "inst_default", "instance_url": base_url, "api_key": "k"})
        test_retention(tmp, base_url, cfg)
        test_restore(tmp, base_url, cfg)
        test_assets_roundtrip(tmp, base_url, cfg)
        test_api_assets(tmp, base_url)
        srv.shutdown()
        test_email(tmp)
        test_api(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
