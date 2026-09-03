"""Runnable self-check for AUT-2285 (scheduler retry window).

The existing test_engine.py drives `_scheduler_step` back-to-back, which
masks a real-world bug: the production scheduler only ticks every 60s, and
`due` was computed from `last_run` (only stamped on success), so a single
transient failure stalled retries for up to a full interval (1h on hosted).

This script simulates the real production cadence: an attempt every 60s
with a constant failing upstream for 5 minutes, then recovery. With the
fix, the scheduler should retry on every tick during the outage, alert
after the 3rd consecutive failure, then recover with status=ok.
"""

import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402


class AlwaysFailHandler(BaseHTTPRequestHandler):
    fail = True

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"detail": "boom"}).encode()
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_srv():
    AlwaysFailHandler.fail = True
    srv = ThreadingHTTPServer(("127.0.0.1", 0), AlwaysFailHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def write_cfg(path, url):
    path.write_text(json.dumps({
        "backup_dir": str(path.parent / "data"),
        "email": {"enabled": False},
        "instances": [{
            "id": "inst_default", "nickname": "hosted",
            "instance_url": url, "api_key": "k",
            "schedule_interval": 3600, "enabled": True,
        }],
    }))


def parse_iso(s):
    return datetime.fromisoformat(s)


def main():
    tmp = Path("/tmp/aut-2285-selfcheck")
    tmp.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp / "cfg.json"
    srv = start_srv()
    base = f"http://127.0.0.1:{srv.server_port}"
    write_cfg(cfg_path, base)
    (tmp / "data" / "inst_default").mkdir(parents=True, exist_ok=True)
    (tmp / "data" / "inst_default" / "state.json").write_text(json.dumps({}))

    # Park the background scheduler so we drive the tick deterministically.
    orig_sched = server.App._scheduler
    server.App._scheduler = lambda self: self._sched_stop.wait()
    try:
        app = server.App(str(cfg_path), str(tmp / "data"))
    finally:
        server.App._scheduler = orig_sched

    inst = app.config.instances()[0]
    eng = app._engine(inst["id"])

    # Simulate the FIRST attempt at t=0, then 5 more ticks (one per 60s) all
    # failing, then a 7th tick where the upstream has recovered.
    t0 = datetime.now(timezone.utc)
    attempt_times = [t0 + timedelta(minutes=i) for i in range(6)]

    import engine
    for i, fake_now in enumerate(attempt_times):
        # Patch both server and engine "now" to simulate real wall-clock ticks.
        orig_srv_dt = server.datetime
        orig_eng_utc = engine._utcnow
        server.datetime = type("F", (orig_srv_dt,), {
            "now": staticmethod(lambda tz=None: fake_now),
        })
        engine._utcnow = lambda: fake_now
        try:
            app._scheduler_step(inst)
        finally:
            server.datetime = orig_srv_dt
            engine._utcnow = orig_eng_utc
        st = eng.state.get()
        print(f"tick {i+1} (t={fake_now.isoformat()}): "
              f"consecutive_failures={st.get('consecutive_failures')} "
              f"last_status={st.get('last_status')} "
              f"last_attempt_at={st.get('last_attempt_at')}")

    final = eng.state.get()
    assert final.get("consecutive_failures") == 6, final
    assert final.get("last_attempt_at"), final
    # With the fix, last_attempt_at must advance on every tick (not stall at t0).
    last_attempt = parse_iso(final["last_attempt_at"])
    assert last_attempt == attempt_times[-1], (last_attempt, attempt_times[-1])
    # last_run gets stamped on the alert threshold (3rd fail) to back off the
    # scheduler after a sustained outage.
    assert final.get("last_run") is not None, final

    # Recovery tick: now the upstream returns 200.
    class OkHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def do_GET(self):
            if self.path.endswith("/assets/backup"):
                # Return a tiny gzip tar so validate_assets passes.
                import gzip, io, tarfile
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                    pass
                body = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            from test_engine import make_backup  # type: ignore
            try:
                payload = make_backup()
            except Exception:
                payload = {
                    "app": "autobrain", "kind": "backup", "version": 1,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "data": {}, "checksum": "0" * 64,
                }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv.shutdown()
    AlwaysFailHandler.fail = False
    srv2 = ThreadingHTTPServer(("127.0.0.1", 0), OkHandler)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    base2 = f"http://127.0.0.1:{srv2.server_port}"
    app.config.update_instance(inst["id"], {"instance_url": base2})

    recovery_t = attempt_times[-1] + timedelta(minutes=1)
    orig_srv_dt = server.datetime
    orig_eng_utc = engine._utcnow
    server.datetime = type("F", (orig_srv_dt,), {
        "now": staticmethod(lambda tz=None: recovery_t),
    })
    engine._utcnow = lambda: recovery_t
    try:
        app._scheduler_step(inst)
    finally:
        server.datetime = orig_srv_dt
        engine._utcnow = orig_eng_utc

    after = eng.state.get()
    print(f"recovery tick: last_status={after.get('last_status')} "
          f"consecutive_failures={after.get('consecutive_failures')}")
    assert after.get("last_status") == "ok", after
    assert after.get("consecutive_failures") == 0, after

    srv2.shutdown()
    app.stop()
    print("OK: scheduler retries every tick on transient failure, "
          "recovers cleanly once the upstream is back (AUT-2285).")


if __name__ == "__main__":
    main()
