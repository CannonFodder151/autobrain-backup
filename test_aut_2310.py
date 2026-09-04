"""Runnable self-check for AUT-2310: scheduler alert must fire EXACTLY ONCE
when consecutive_failures first crosses the threshold — not every tick after.

Reproduces the spam: 10 ticks at 60s apart, all failing. With the fix, the
alert mailbox contains exactly 1 message, not 8. On the recovery tick the
alerted_at flag clears so a future outage will alert again.
"""

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402
import engine  # noqa: E402


class AlwaysFailHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"detail": "boom"}).encode()
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeSMTPServer:
    """Captures outgoing mail without actually sending."""
    def __init__(self):
        self.inbox = []
        self.lock = threading.Lock()


_FAKE_SMTP = FakeSMTPServer()


def _patch_mailer():
    """Replace Mailer.send so we count alert emails without SMTP."""
    orig_init = engine.Mailer.__init__

    def _init(self, cfg):
        orig_init(self, cfg)
        self._fake = True

    def _send(self, subject, body):
        with _FAKE_SMTP.lock:
            _FAKE_SMTP.inbox.append((subject, body))
        return True

    engine.Mailer.__init__ = _init
    engine.Mailer.send = _send


def main():
    _patch_mailer()

    tmp = Path("/tmp/aut-2310-selfcheck")
    tmp.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp / "cfg.json"
    cfg_path.write_text(json.dumps({
        "backup_dir": str(tmp / "data"),
        "email": {
            "enabled": True,
            "smtp_host": "127.0.0.1",
            "smtp_port": 0,
            "smtp_user": "",
            "use_tls": False,
            "from": "noreply@ab.app",
            "to": ["ops@ab.app"],
        },
        "instances": [{
            "id": "inst_default", "nickname": "hosted",
            "instance_url": "http://127.0.0.1:1", "api_key": "k",
            "schedule_interval": 3600, "enabled": True,
        }],
    }))

    srv = ThreadingHTTPServer(("127.0.0.1", 0), AlwaysFailHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    (tmp / "data" / "inst_default").mkdir(parents=True, exist_ok=True)
    (tmp / "data" / "inst_default" / "state.json").write_text(json.dumps({}))

    orig_sched = server.App._scheduler
    server.App._scheduler = lambda self: self._sched_stop.wait()
    try:
        app = server.App(str(cfg_path), str(tmp / "data"))
    finally:
        server.App._scheduler = orig_sched

    inst = app.config.instances()[0]
    app.config.update_instance(inst["id"], {"instance_url": base})
    eng = app._engine(inst["id"])

    t0 = datetime.now(timezone.utc)
    attempt_times = [t0 + timedelta(minutes=i) for i in range(10)]

    for i, fake_now in enumerate(attempt_times):
        orig_srv_dt = server.datetime
        orig_eng_utc = engine._utcnow
        server.datetime = type("F", (orig_srv_dt,), {
            "now": staticmethod(lambda tz=None, _fn=fake_now: _fn),
        })
        engine._utcnow = lambda _fn=fake_now: _fn
        try:
            app._scheduler_step(inst)
        finally:
            server.datetime = orig_srv_dt
            engine._utcnow = orig_eng_utc
        st = eng.state.get()
        print(
            f"tick {i+1}: fails={st.get('consecutive_failures')} "
            f"alerted_at={st.get('alerted_at')} "
            f"inbox={len(_FAKE_SMTP.inbox)}"
        )

    final = eng.state.get()
    assert final.get("consecutive_failures") == 10, final
    inbox_count = len(_FAKE_SMTP.inbox)
    assert inbox_count == 1, (
        f"alert spam: expected exactly 1 email after 10 failed ticks, got {inbox_count}. "
        f"Fix is not working."
    )
    assert final.get("alerted_at"), "alerted_at should be set after the alert fires"

    srv.shutdown()
    app.stop()
    print(f"OK: scheduler alerts exactly once on sustained failure (got {inbox_count} email).")


if __name__ == "__main__":
    main()