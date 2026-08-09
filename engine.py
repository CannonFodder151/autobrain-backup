"""AutoBrain backup engine.

Deterministic, stdlib-only backup logic for the autobrain-backup service:

  * pull   - fetch a snapshot from an AutoBrain instance (admin API key)
  * save   - validate the payload is a genuine AutoBrain backup, store it
  * rotate - combine raw backups into hourly/daily/weekly retention tiers
  * restore - push a stored backup back into an AutoBrain instance
  * mail   - SMTP alerts on job failure and corruption (stdlib smtplib)

Backups are full database snapshots, so "combine" means retention tiers:
the hourly tier keeps the most recent snapshots, the daily tier keeps one
per day, and the weekly tier keeps one per week. Older tiers prune to the
configured limits.
"""

import copy
import json
import os
import re
import shutil
import smtplib
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

APP_NAME = "autobrain"
KIND_BACKUP = "backup"
HEADER_KEY = "X-Admin-API-Key"

DEFAULT_CONFIG = {
    "instance_url": "",
    "api_key": "",
    "backup_endpoint": "/admin-api/backup",
    "restore_endpoint": "/admin-api/restore",
    "backup_dir": "/backups",
    "schedule_interval": 3600,
    "gui_key": "",
    "gui_user": "",
    "gui_password": "",
    "ingest_key": "",
    "retention": {"hourly": 24, "daily": 30, "weekly": 12},
    "email": {
        "enabled": False,
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_user": "",
        "smtp_password": "",
        "use_tls": True,
        "from": "",
        "to": [],
    },
}

MASKED_KEYS = {"api_key", "gui_key", "gui_password", "ingest_key", "smtp_password"}
_HDRS = {"Accept": "application/json", "User-Agent": "autobrain-backup/1.0"}


class BackupError(Exception):
    pass


def _utcnow():
    return datetime.now(timezone.utc)


def _stamp(dt=None):
    dt = dt or _utcnow()
    return dt.strftime("%Y%m%d-%H%M%S")


def _daykey(dt=None):
    dt = dt or _utcnow()
    return dt.strftime("%Y%m%d")


def _weekkey(dt=None):
    dt = dt or _utcnow()
    iso = dt.isocalendar()
    return f"{iso[0]}W{iso[1]:02d}"


class Mailer:
    """Thin smtplib wrapper; never raises (alerts must not kill the backup loop)."""

    def __init__(self, cfg):
        self.cfg = cfg.get("email") or {}

    def enabled(self):
        return bool(self.cfg.get("enabled") and self.cfg.get("smtp_host"))

    def send(self, subject, body):
        if not self.enabled():
            return False
        to = self.cfg.get("to") or []
        if isinstance(to, str):
            to = [t.strip() for t in to.split(",") if t.strip()]
        if not to:
            return False
        try:
            msg = (
                f"From: {self.cfg.get('from') or self.cfg.get('smtp_user')}\n"
                f"To: {', '.join(to)}\n"
                f"Subject: {subject}\n\n"
                f"{body}\n"
            )
            if self.cfg.get("use_tls", True):
                with smtplib.SMTP(self.cfg["smtp_host"], int(self.cfg.get("smtp_port", 587)), timeout=30) as s:
                    s.ehlo()
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                    if self.cfg.get("smtp_user"):
                        s.login(self.cfg["smtp_user"], self.cfg.get("smtp_password", ""))
                    s.sendmail(self.cfg.get("from") or self.cfg["smtp_user"], to, msg)
            else:
                with smtplib.SMTP(self.cfg["smtp_host"], int(self.cfg.get("smtp_port", 587)), timeout=30) as s:
                    if self.cfg.get("smtp_user"):
                        s.login(self.cfg["smtp_user"], self.cfg.get("smtp_password", ""))
                    s.sendmail(self.cfg.get("from") or self.cfg["smtp_user"], to, msg)
            return True
        except Exception:
            return False


class Config:
    """Config file lives on the docker host (mounted folder); edited via GUI."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cfg = self._load()

    def _load(self):
        if self.path.exists():
            try:
                return self._merge(copy.deepcopy(DEFAULT_CONFIG), json.loads(self.path.read_text("utf-8")))
            except (json.JSONDecodeError, OSError):
                return copy.deepcopy(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)

    @staticmethod
    def _merge(base, overlay):
        for k, v in overlay.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                base[k] = Config._merge(base[k], v)
            else:
                base[k] = v
        return base

    def get(self):
        with self._lock:
            return copy.deepcopy(self._cfg)

    def masked(self):
        cfg = self.get()
        for k, v in cfg.items():
            if k in MASKED_KEYS and v:
                cfg[k] = "********"
        for k, v in (cfg.get("email") or {}).items():
            if k in MASKED_KEYS and v:
                cfg["email"][k] = "********"
        return cfg

    def save(self, new_cfg):
        new_cfg = self._merge(self.get(), new_cfg)
        for k in ("instance_url", "api_key", "backup_endpoint", "restore_endpoint"):
            new_cfg[k] = str(new_cfg.get(k) or "").strip()
        new_cfg["backup_dir"] = str(new_cfg.get("backup_dir") or "").strip() or "/backups"
        try:
            new_cfg["schedule_interval"] = max(60, int(new_cfg.get("schedule_interval", 3600)))
        except (TypeError, ValueError):
            new_cfg["schedule_interval"] = 3600
        for tier in ("hourly", "daily", "weekly"):
            try:
                new_cfg["retention"][tier] = max(0, int(new_cfg.get("retention", {}).get(tier, DEFAULT_CONFIG["retention"][tier])))
            except (TypeError, ValueError):
                new_cfg["retention"][tier] = DEFAULT_CONFIG["retention"][tier]
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(new_cfg, indent=2), "utf-8")
            os.replace(tmp, self.path)
            self._cfg = new_cfg
        return new_cfg


class State:
    """Persistent run state: last run, health, archive promotion cursors."""

    FIELDS = {"last_run", "last_status", "last_error", "last_backup_at",
              "next_run_at", "last_daily_date", "last_weekly_date", "counters"}

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._s = {}
        if self.path.exists():
            try:
                self._s = json.loads(self.path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                self._s = {}

    def get(self):
        with self._lock:
            return json.loads(json.dumps(self._s))

    def update(self, **kw):
        with self._lock:
            for k, v in kw.items():
                if k in self.FIELDS:
                    self._s[k] = v
            self._persist()

    def touch_counters(self, ok=True):
        with self._lock:
            c = self._s.setdefault("counters", {})
            c["total"] = c.get("total", 0) + 1
            c["ok"] = c.get("ok", 0) + (1 if ok else 0)
            c["fail"] = c.get("fail", 0) + (0 if ok else 1)
            self._persist()

    def _persist(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._s, indent=2), "utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass


class BackupEngine:
    def __init__(self, config, state, backup_dir=None):
        self.config = config
        self.state = state
        self.backup_dir = Path(backup_dir or config.get().get("backup_dir") or "/backups")

    # --- transport ---
    def _opener(self):
        return urllib.request.build_opener()

    def _fetch(self, url, key):
        req = urllib.request.Request(url, headers=dict(_HDRS, **{HEADER_KEY: key}))
        try:
            with self._opener().open(req, timeout=120) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raise BackupError(f"server returned HTTP {e.code} for {url}") from e
        except urllib.error.URLError as e:
            raise BackupError(f"cannot reach {url}: {e.reason}") from e

    # --- validation ---
    @staticmethod
    def validate(body):
        """Ensure the payload really is an AutoBrain backup; returns parsed dict."""
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise BackupError("payload is not valid JSON") from e
        if not isinstance(data, dict):
            raise BackupError("payload is not a JSON object")
        if data.get("app") != APP_NAME:
            raise BackupError(f"not an {APP_NAME} backup: app={data.get('app')!r}")
        if data.get("kind") != KIND_BACKUP:
            raise BackupError(f"not a full backup: kind={data.get('kind')!r}")
        if not isinstance(data.get("data"), dict):
            raise BackupError("backup missing data section")
        return data

    # --- storage ---
    def _path(self, *parts):
        p = self.backup_dir.joinpath(*parts)
        # guard: never escape the backup root
        if self.backup_dir.resolve() not in p.resolve().parents and p.resolve() != self.backup_dir.resolve():
            raise BackupError("path escapes backup directory")
        return p

    def _hourly_dir(self):
        return self._path("hourly")

    def _daily_dir(self):
        return self._path("daily")

    def _weekly_dir(self):
        return self._path("weekly")

    def _save_body(self, body):
        data = self.validate(body)
        created = data.get("created_at") or _utcnow().isoformat()
        stamp = re.sub(r"[^0-9]", "", str(created))[:14]
        if not stamp:
            stamp = _stamp()
        hourly = self._hourly_dir()
        hourly.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".backup-", suffix=".json", dir=hourly)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            name = f"autobrain-backup-{stamp}.json"
            dest = hourly / name
            if dest.exists():
                dest.unlink()
            shutil.move(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return dest, data

    def run_backup(self):
        """Fetch, validate, save and rotate. Returns the saved backup name."""
        cfg = self.config.get()
        url = str(cfg.get("instance_url") or "").rstrip("/")
        key = cfg.get("api_key") or ""
        if not url or not key:
            raise BackupError("instance_url and api_key are required in config")
        endpoint = str(cfg.get("backup_endpoint") or "/admin-api/backup")
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        body, _ct = self._fetch(url + endpoint, key)
        path, data = self._save_body(body)
        self.rotate()
        self.state.update(last_run=_utcnow().isoformat(), last_status="ok",
                          last_error=None, last_backup_at=path.name)
        self.state.touch_counters(ok=True)
        return path.name

    def ingest(self, body, expected_key):
        """Accept a backup pushed by the autobrain-backup-agent."""
        cfg = self.config.get()
        configured = cfg.get("ingest_key") or ""
        if expected_key and configured and expected_key != configured:
            raise BackupError("invalid ingest key")
        path, data = self._save_body(body)
        self.rotate()
        self.state.update(last_run=_utcnow().isoformat(), last_status="ok",
                          last_error=None, last_backup_at=path.name)
        self.state.touch_counters(ok=True)
        return path.name

    def rotate(self):
        """Promote hourly -> daily (once/day) and daily -> weekly (once/week), then prune."""
        cfg = self.config.get()
        ret = cfg.get("retention") or {}
        hourly = self._hourly_dir()
        hourly.mkdir(parents=True, exist_ok=True)
        daily = self._daily_dir()
        daily.mkdir(parents=True, exist_ok=True)
        weekly = self._weekly_dir()
        weekly.mkdir(parents=True, exist_ok=True)

        st = self.state.get()
        today = _daykey()
        week = _weekkey()

        if st.get("last_daily_date") != today:
            newest = sorted(hourly.glob("autobrain-backup-*.json"))[-1:] if any(hourly.iterdir()) else []
            if newest:
                src = newest[0]
                day_name = f"autobrain-backup-daily-{today}.json"
                shutil.copy2(src, daily / day_name)
                self.state.update(last_daily_date=today)

        if st.get("last_weekly_date") != week:
            newest = sorted(daily.glob("autobrain-backup-daily-*.json"))[-1:] if any(daily.iterdir()) else []
            if not newest:
                newest = sorted(hourly.glob("autobrain-backup-*.json"))[-1:] if any(hourly.iterdir()) else []
            if newest:
                src = newest[0]
                week_name = f"autobrain-backup-weekly-{week}.json"
                shutil.copy2(src, weekly / week_name)
                self.state.update(last_weekly_date=week)

        self._prune(hourly, "autobrain-backup-*.json", int(ret.get("hourly", 24)))
        self._prune(daily, "autobrain-backup-daily-*.json", int(ret.get("daily", 30)))
        self._prune(weekly, "autobrain-backup-weekly-*.json", int(ret.get("weekly", 12)))

    @staticmethod
    def _prune(directory, pattern, keep):
        if keep <= 0:
            return
        files = sorted(directory.glob(pattern))
        for old in files[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass

    # --- restore ---
    def restore(self, source, confirm=False):
        """Push a stored backup back to the instance. Wipes existing data."""
        if not confirm:
            raise BackupError("restore requires confirm=true")
        cfg = self.config.get()
        url = str(cfg.get("instance_url") or "").rstrip("/")
        key = cfg.get("api_key") or ""
        if not url or not key:
            raise BackupError("instance_url and api_key are required in config")
        path = self._resolve_source(source)
        body = path.read_bytes()
        self.validate(body)  # corruption gate before we wipe anything
        endpoint = str(cfg.get("restore_endpoint") or "/admin-api/restore")
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        boundary = "----autobrainbackup"
        payload = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="restore.json"\r\n'
            "Content-Type: application/json\r\n\r\n"
        ).encode("utf-8") + body + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            url + endpoint,
            data=payload,
            method="POST",
            headers=dict(_HDRS, **{
                HEADER_KEY: key,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            }),
        )
        try:
            with self._opener().open(req, timeout=300) as r:
                r.read()
                return r.status, path.name
        except urllib.error.HTTPError as e:
            raise BackupError(f"restore failed: server returned HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise BackupError(f"restore failed: cannot reach {url}: {e.reason}") from e

    def _resolve_source(self, source):
        """Resolve a stored backup name or upload filename to a concrete file."""
        if isinstance(source, dict):  # upload: {"filename": str, "content_b64": str}
            body = __import__("base64").b64decode(source.get("content_b64", ""))
            data = self.validate(body)
            name = f"autobrain-backup-{re.sub(r'[^0-9]', '', data.get('created_at') or _stamp())[:14]}.json"
            path = self._hourly_dir() / name
            path.write_bytes(body)
            return path
        name = str(source)
        if name.startswith(".") or "/" in name or "\\" in name:
            raise BackupError("invalid backup name")
        for d in (self._hourly_dir(), self._daily_dir(), self._weekly_dir()):
            p = d / name
            if p.exists():
                return p
        raise BackupError("backup not found")

    def list_backups(self):
        out = {"hourly": [], "daily": [], "weekly": []}
        for tier, pattern in (("hourly", "autobrain-backup-*.json"),
                              ("daily", "autobrain-backup-daily-*.json"),
                              ("weekly", "autobrain-backup-weekly-*.json")):
            d = self._path(tier)
            if d.exists():
                for p in sorted(d.glob(pattern), reverse=True):
                    try:
                        mtime = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()
                    except OSError:
                        mtime = None
                    out[tier].append({"name": p.name, "size": p.stat().st_size, "mtime": mtime})
        return out

    def status(self):
        cfg = self.config.get()
        st = self.state.get()
        disk = shutil.disk_usage(self.backup_dir) if self.backup_dir.exists() else None
        return {
            "instance": cfg.get("instance_url") or None,
            "configured": bool(cfg.get("instance_url") and cfg.get("api_key")),
            "email_enabled": self._mailer(cfg).enabled(),
            "ingest_enabled": bool(cfg.get("ingest_key")),
            "schedule_interval": cfg.get("schedule_interval"),
            "retention": cfg.get("retention"),
            "last_run": st.get("last_run"),
            "last_status": st.get("last_status"),
            "last_error": st.get("last_error"),
            "last_backup_at": st.get("last_backup_at"),
            "next_run_at": st.get("next_run_at"),
            "counters": st.get("counters", {}),
            "backup_dir": str(self.backup_dir),
            "disk": None if disk is None else {
                "total": disk.total, "used": disk.used, "free": disk.free,
            },
            "backups": {t: len(v) for t, v in self.list_backups().items()},
        }

    def _mailer(self, cfg=None):
        return Mailer(cfg or self.config.get())

    def alert_failure(self, error):
        self._mailer().send("[AutoBrain Backup] job failed", f"A backup run failed:\n\n{error}")

    def alert_corruption(self, error):
        self._mailer().send("[AutoBrain Backup] corrupt backup detected", f"A corrupted or invalid backup was rejected:\n\n{error}")

    def alert_restore(self, status, name):
        self._mailer().send("[AutoBrain Backup] restore completed",
                            f"Restore of {name} completed with HTTP {status}.")
