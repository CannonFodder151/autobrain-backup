"""AutoBrain backup engine.

Deterministic, stdlib-only backup logic for the autobrain-backup service:

  * pull   - fetch a snapshot from an AutoBrain instance (admin API key)
  * save   - validate the payload is a genuine AutoBrain backup, store it
  * rotate - combine raw backups into hourly/daily/weekly retention tiers
  * restore - push a stored backup back into an AutoBrain instance
  * mail   - SMTP alerts on job failure and corruption (stdlib smtplib)

Multi-tenant: one config file holds a list of AutoBrain instances. Each
instance has its own nickname, admin credentials, retention, schedule and
backup folder (``{backup_dir}/{instance_id}`` by default). Run state for each
instance lives in ``{backup_dir}/{instance_id}/state.json``.

Backups are full database snapshots plus a gzipped tar archive of the
instance's MinIO image assets (same stamp, stored under per-tier `images/`),
so "combine" means retention tiers: the hourly tier keeps the most recent
snapshots, the daily tier keeps one per day, and the weekly tier keeps one
per week. Older tiers prune to the configured limits.
"""

import copy
import json
import os
import re
import secrets
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
    "version": 2,
    "gui_key": "",
    "gui_user": "",
    "gui_password": "",
    "ingest_key": "",
    "backup_dir": "/backups",
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
    "instances": [],
}

DEFAULT_INSTANCE = {
    "nickname": "",
    "instance_url": "",
    "api_key": "",
    "backup_endpoint": "/api/v1/admin-api/backup",
    "restore_endpoint": "/api/v1/admin-api/restore",
    "assets_backup_endpoint": "/api/v1/admin-api/assets/backup",
    "assets_restore_endpoint": "/api/v1/admin-api/assets/restore",
    "schedule_interval": 3600,
    "ingest_key": "",
    "retention": {"hourly": 24, "daily": 30, "weekly": 12},
    "enabled": True,
    "backup_dir": "",
}

MASKED_KEYS = {"api_key", "gui_key", "gui_password", "ingest_key", "smtp_password"}
_HDRS = {"Accept": "application/json", "User-Agent": "autobrain-backup/3.0.0"}


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
        return self._send(subject, body)[0]

    def test(self):
        return self._send("[AutoBrain Backup] test email",
                          "This is a test email from AutoBrain Backup.\n\n"
                          f"Sent {_utcnow().isoformat()}.")

    def _send(self, subject, body):
        if not self.enabled():
            return False, "email alerts are disabled"
        to = self.cfg.get("to") or []
        if isinstance(to, str):
            to = [t.strip() for t in to.split(",") if t.strip()]
        if not to:
            return False, "no recipients configured"
        from_addr = self.cfg.get("from") or self.cfg.get("smtp_user") or ""
        if not from_addr:
            return False, "no sender address configured"
        try:
            msg = (
                f"From: {from_addr}\n"
                f"To: {', '.join(to)}\n"
                f"Subject: {subject}\n\n"
                f"{body}\n"
            )
            host = self.cfg["smtp_host"]
            port = int(self.cfg.get("smtp_port", 587))
            if self.cfg.get("use_tls", True):
                with smtplib.SMTP(host, port, timeout=30) as s:
                    s.ehlo()
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                    if self.cfg.get("smtp_user"):
                        s.login(self.cfg["smtp_user"], self.cfg.get("smtp_password", ""))
                    s.sendmail(from_addr, to, msg)
            else:
                with smtplib.SMTP(host, port, timeout=30) as s:
                    if self.cfg.get("smtp_user"):
                        s.login(self.cfg["smtp_user"], self.cfg.get("smtp_password", ""))
                    s.sendmail(from_addr, to, msg)
            return True, None
        except Exception as e:
            return False, str(e)


def _merge(base, overlay):
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _merge(base[k], v)
        else:
            base[k] = v
    return base


class Config:
    """Config file lives on the docker host (mounted folder); edited via GUI.

    v2 format: a top-level ``instances`` list plus global GUI auth and SMTP
    alert settings. An existing v1 single-instance config is migrated into a
    single instance on load (data stays in place).
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cfg = self._load()

    def _load(self):
        base = copy.deepcopy(DEFAULT_CONFIG)
        raw = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        base = _merge(base, raw)
        instances = []
        if isinstance(raw.get("instances"), list):
            for inst in raw["instances"]:
                if isinstance(inst, dict):
                    instances.append(self.normalize_instance(inst))
        elif raw.get("instance_url"):
            # v1 single-instance config -> wrap into one instance, keep its data dir
            inst = {
                "id": "inst_default",
                "nickname": raw.get("nickname", ""),
                "instance_url": raw.get("instance_url", ""),
                "api_key": raw.get("api_key", ""),
                "backup_endpoint": raw.get("backup_endpoint") or DEFAULT_INSTANCE["backup_endpoint"],
                "restore_endpoint": raw.get("restore_endpoint") or DEFAULT_INSTANCE["restore_endpoint"],
                "schedule_interval": raw.get("schedule_interval", 3600),
                "ingest_key": raw.get("ingest_key", ""),
                "retention": raw.get("retention") or copy.deepcopy(DEFAULT_INSTANCE["retention"]),
                "enabled": True,
                "backup_dir": raw.get("backup_dir", ""),
            }
            instances.append(self.normalize_instance(inst))
        base["instances"] = instances
        base["version"] = 2
        return base

    @staticmethod
    def normalize_instance(inst):
        out = {}
        inst = inst or {}
        out["id"] = str(inst.get("id") or "inst_" + secrets.token_hex(6))
        out["nickname"] = str(inst.get("nickname") or "").strip()
        out["instance_url"] = str(inst.get("instance_url") or "").strip().rstrip("/")
        out["api_key"] = str(inst.get("api_key") or "")
        out["backup_endpoint"] = str(inst.get("backup_endpoint") or DEFAULT_INSTANCE["backup_endpoint"]).strip()
        out["restore_endpoint"] = str(inst.get("restore_endpoint") or DEFAULT_INSTANCE["restore_endpoint"]).strip()
        out["assets_backup_endpoint"] = str(inst.get("assets_backup_endpoint") or DEFAULT_INSTANCE["assets_backup_endpoint"]).strip()
        out["assets_restore_endpoint"] = str(inst.get("assets_restore_endpoint") or DEFAULT_INSTANCE["assets_restore_endpoint"]).strip()
        for e in ("backup_endpoint", "restore_endpoint",
                  "assets_backup_endpoint", "assets_restore_endpoint"):
            if not out[e].startswith("/"):
                out[e] = "/" + out[e]
        try:
            out["schedule_interval"] = max(60, int(inst.get("schedule_interval", 3600)))
        except (TypeError, ValueError):
            out["schedule_interval"] = 3600
        out["ingest_key"] = str(inst.get("ingest_key") or "")
        ret = {}
        src = inst.get("retention") or {}
        if not isinstance(src, dict):
            src = {}
        for tier in ("hourly", "daily", "weekly"):
            try:
                ret[tier] = max(0, int(src.get(tier, DEFAULT_INSTANCE["retention"][tier])))
            except (TypeError, ValueError):
                ret[tier] = DEFAULT_INSTANCE["retention"][tier]
        out["retention"] = ret
        out["enabled"] = bool(inst.get("enabled", True))
        out["backup_dir"] = str(inst.get("backup_dir") or "").strip()
        return out

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
        for inst in cfg.get("instances") or []:
            for k, v in inst.items():
                if k in MASKED_KEYS and v:
                    inst[k] = "********"
        return cfg

    def instances(self):
        return self.get().get("instances") or []

    def get_instance(self, iid):
        for inst in self.instances():
            if inst.get("id") == iid:
                return inst
        return None

    def add_instance(self, data):
        inst = self.normalize_instance(dict(data or {}))
        with self._lock:
            self._cfg["instances"].append(inst)
            self._persist()
        return inst

    def update_instance(self, iid, data):
        with self._lock:
            for i, inst in enumerate(self._cfg.get("instances") or []):
                if inst.get("id") != iid:
                    continue
                merged = _merge(inst, dict(data or {}))
                self._cfg["instances"][i] = self.normalize_instance(merged)
                self._persist()
                return self._cfg["instances"][i]
        return None

    def remove_instance(self, iid):
        with self._lock:
            before = len(self._cfg.get("instances") or [])
            self._cfg["instances"] = [i for i in self._cfg.get("instances") or [] if i.get("id") != iid]
            if len(self._cfg["instances"]) != before:
                self._persist()
                return True
        return False

    def save(self, new_cfg):
        new_cfg = _merge(self.get(), new_cfg)
        for k in ("gui_key", "gui_user", "gui_password", "ingest_key"):
            if isinstance(new_cfg.get(k), str):
                new_cfg[k] = new_cfg[k].strip()
        new_cfg["backup_dir"] = str(new_cfg.get("backup_dir") or "").strip() or "/backups"
        email = new_cfg.get("email") or {}
        if isinstance(email, dict):
            email["smtp_host"] = str(email.get("smtp_host") or "").strip()
            try:
                email["smtp_port"] = max(1, int(email.get("smtp_port", 587)))
            except (TypeError, ValueError):
                email["smtp_port"] = 587
            email["smtp_user"] = str(email.get("smtp_user") or "").strip()
            email["from"] = str(email.get("from") or "").strip()
            email["use_tls"] = bool(email.get("use_tls", True))
            email["enabled"] = bool(email.get("enabled"))
            to = email.get("to") or []
            if isinstance(to, str):
                to = [t.strip() for t in to.split(",") if t.strip()]
            email["to"] = [str(t).strip() for t in to if str(t).strip()]
            new_cfg["email"] = email
        if isinstance(new_cfg.get("instances"), list):
            new_cfg["instances"] = [self.normalize_instance(i) for i in new_cfg["instances"] if isinstance(i, dict)]
        new_cfg["version"] = 2
        with self._lock:
            self._cfg = new_cfg
            self._persist()
        return new_cfg

    def _persist(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cfg, indent=2), "utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass


class State:
    """Persistent per-instance run state: last run, health, archive cursors."""

    FIELDS = {"last_run", "last_status", "last_error", "last_backup_at",
              "next_run_at", "last_daily_date", "last_weekly_date", "counters",
              "last_assets_at", "last_assets_error"}

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
    def __init__(self, instance, state, backup_dir, email_cfg):
        self.instance = instance
        self.state = state
        self.backup_dir = Path(backup_dir)
        self.email_cfg = email_cfg or {}

    def _cfg(self, key, default=None):
        return self.instance.get(key, default)

    def label(self):
        return self._cfg("nickname") or self._cfg("instance_url") or self.instance.get("id", "?")

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

    @staticmethod
    def validate_assets(body):
        """Ensure the payload really is a gzipped tar of image objects."""
        import io
        import tarfile

        if not body or body[:2] != b"\x1f\x8b":
            raise BackupError("asset archive is not a gzip tar")
        try:
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
                names = tar.getnames()
        except (tarfile.TarError, EOFError, OSError) as e:
            raise BackupError(f"invalid image archive: {e}") from e
        for name in names:
            if name.startswith("/") or ".." in name.split("/"):
                raise BackupError("image archive contains an unsafe member name")
        return names

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

    def _save_assets(self, body, stamp):
        """Validate and store an image archive under hourly/images/."""
        self.validate_assets(body)
        images = self._hourly_dir() / "images"
        images.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".assets-", suffix=".tar.gz", dir=images)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            name = f"autobrain-assets-{stamp}.tar.gz"
            dest = images / name
            if dest.exists():
                dest.unlink()
            shutil.move(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return dest

    def run_backup(self):
        """Fetch, validate, save and rotate. Returns the saved backup name."""
        url = str(self._cfg("instance_url") or "").rstrip("/")
        key = self._cfg("api_key") or ""
        if not url or not key:
            raise BackupError("instance_url and api_key are required in config")
        endpoint = str(self._cfg("backup_endpoint") or "/api/v1/admin-api/backup")
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        body, _ct = self._fetch(url + endpoint, key)
        path, data = self._save_body(body)
        stamp = path.stem[len("autobrain-backup-"):]
        assets_error = None
        assets_at = None
        try:
            aep = str(self._cfg("assets_backup_endpoint") or "/api/v1/admin-api/assets/backup")
            if not aep.startswith("/"):
                aep = "/" + aep
            abody, _ct = self._fetch(url + aep, key)
            self._save_assets(abody, stamp)
            assets_at = _utcnow().isoformat()
        except BackupError as e:
            assets_error = str(e)
        self.rotate()
        self.state.update(last_run=_utcnow().isoformat(), last_status="ok",
                          last_error=None, last_backup_at=path.name,
                          last_assets_at=assets_at, last_assets_error=assets_error)
        self.state.touch_counters(ok=True)
        return path.name

    def ingest(self, body, expected_key):
        """Accept a backup pushed by the autobrain-backup-agent for this instance."""
        configured = self._cfg("ingest_key") or ""
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
        ret = self._cfg("retention") or {}
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
            newest_img = sorted((hourly / "images").glob("autobrain-assets-*.tar.gz"))[-1:]
            if newest_img:
                aimg = daily / "images"
                aimg.mkdir(parents=True, exist_ok=True)
                shutil.copy2(newest_img[0], aimg / f"autobrain-assets-daily-{today}.tar.gz")
            if newest or newest_img:
                self.state.update(last_daily_date=today)

        if st.get("last_weekly_date") != week:
            newest = sorted(daily.glob("autobrain-backup-daily-*.json"))[-1:] if any(daily.iterdir()) else []
            if not newest:
                newest = sorted(hourly.glob("autobrain-backup-*.json"))[-1:] if any(hourly.iterdir()) else []
            if newest:
                src = newest[0]
                week_name = f"autobrain-backup-weekly-{week}.json"
                shutil.copy2(src, weekly / week_name)
            newest_img = sorted((daily / "images").glob("autobrain-assets-daily-*.tar.gz"))[-1:]
            if not newest_img:
                newest_img = sorted((hourly / "images").glob("autobrain-assets-*.tar.gz"))[-1:]
            if newest_img:
                aimg = weekly / "images"
                aimg.mkdir(parents=True, exist_ok=True)
                shutil.copy2(newest_img[0], aimg / f"autobrain-assets-weekly-{week}.tar.gz")
            if newest or newest_img:
                self.state.update(last_weekly_date=week)

        self._prune(hourly, "autobrain-backup-*.json", int(ret.get("hourly", 24)))
        self._prune(daily, "autobrain-backup-daily-*.json", int(ret.get("daily", 30)))
        self._prune(weekly, "autobrain-backup-weekly-*.json", int(ret.get("weekly", 12)))
        self._prune(hourly / "images", "autobrain-assets-*.tar.gz", int(ret.get("hourly", 24)))
        self._prune(daily / "images", "autobrain-assets-daily-*.tar.gz", int(ret.get("daily", 30)))
        self._prune(weekly / "images", "autobrain-assets-weekly-*.tar.gz", int(ret.get("weekly", 12)))

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
        url = str(self._cfg("instance_url") or "").rstrip("/")
        key = self._cfg("api_key") or ""
        if not url or not key:
            raise BackupError("instance_url and api_key are required in config")
        path = self._resolve_source(source)
        body = path.read_bytes()
        self.validate(body)  # corruption gate before we wipe anything
        endpoint = str(self._cfg("restore_endpoint") or "/api/v1/admin-api/restore")
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
        if isinstance(source, dict):  # {"name": str} or upload {"filename", "content_b64"}
            if source.get("content_b64"):
                body = __import__("base64").b64decode(source.get("content_b64", ""))
                data = self.validate(body)
                name = f"autobrain-backup-{re.sub(r'[^0-9]', '', data.get('created_at') or _stamp())[:14]}.json"
                path = self._hourly_dir() / name
                path.write_bytes(body)
                return path
            source = source.get("name")
        name = str(source)
        if name.startswith(".") or "/" in name or "\\" in name:
            raise BackupError("invalid backup name")
        for d in (self._hourly_dir(), self._daily_dir(), self._weekly_dir()):
            p = d / name
            if p.exists():
                return p
        raise BackupError("backup not found")

    def _resolve_assets_source(self, source):
        """Resolve a stored image archive name or upload dict to a concrete file."""
        if isinstance(source, dict):  # {"name": str} or upload {"filename", "content_b64"}
            if source.get("content_b64"):
                body = __import__("base64").b64decode(source.get("content_b64", ""))
                self.validate_assets(body)
                name = f"autobrain-assets-{_stamp()}.tar.gz"
                path = self._hourly_dir() / "images" / name
                path.write_bytes(body)
                return path
            source = source.get("name")
        name = str(source)
        if name.startswith(".") or "/" in name or "\\" in name:
            raise BackupError("invalid image archive name")
        for d in (self._hourly_dir(), self._daily_dir(), self._weekly_dir()):
            p = d / "images" / name
            if p.exists():
                return p
        raise BackupError("image archive not found")

    def restore_assets(self, source, confirm=False):
        """Push a stored image archive back to the instance. Wipes existing images."""
        if not confirm:
            raise BackupError("restore requires confirm=true")
        url = str(self._cfg("instance_url") or "").rstrip("/")
        key = self._cfg("api_key") or ""
        if not url or not key:
            raise BackupError("instance_url and api_key are required in config")
        path = self._resolve_assets_source(source)
        body = path.read_bytes()
        self.validate_assets(body)  # corruption gate before we wipe anything
        endpoint = str(self._cfg("assets_restore_endpoint") or "/api/v1/admin-api/assets/restore")
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        boundary = "----autobrainbackup"
        payload = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="restore.tar.gz"\r\n'
            "Content-Type: application/gzip\r\n\r\n"
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
            with self._opener().open(req, timeout=600) as r:
                r.read()
                return r.status, path.name
        except urllib.error.HTTPError as e:
            raise BackupError(f"image restore failed: server returned HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise BackupError(f"image restore failed: cannot reach {url}: {e.reason}") from e

    def list_backups(self):
        out = {"hourly": [], "daily": [], "weekly": [], "images": {"hourly": [], "daily": [], "weekly": []}}
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
        for tier, pattern in (("hourly", "autobrain-assets-*.tar.gz"),
                              ("daily", "autobrain-assets-daily-*.tar.gz"),
                              ("weekly", "autobrain-assets-weekly-*.tar.gz")):
            d = self._path(tier) / "images"
            if d.exists():
                for p in sorted(d.glob(pattern), reverse=True):
                    try:
                        mtime = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()
                    except OSError:
                        mtime = None
                    out["images"][tier].append({"name": p.name, "size": p.stat().st_size, "mtime": mtime})
        return out

    def status(self):
        st = self.state.get()
        disk = shutil.disk_usage(self.backup_dir) if self.backup_dir.exists() else None
        return {
            "id": self.instance.get("id"),
            "nickname": self._cfg("nickname"),
            "instance": self._cfg("instance_url") or None,
            "configured": bool(self._cfg("instance_url") and self._cfg("api_key")),
            "enabled": self._cfg("enabled", True),
            "email_enabled": self._mailer().enabled(),
            "ingest_enabled": bool(self._cfg("ingest_key")),
            "schedule_interval": self._cfg("schedule_interval"),
            "retention": self._cfg("retention"),
            "last_run": st.get("last_run"),
            "last_status": st.get("last_status"),
            "last_error": st.get("last_error"),
            "last_backup_at": st.get("last_backup_at"),
            "next_run_at": st.get("next_run_at"),
            "counters": st.get("counters", {}),
            "assets": {
                "last_assets_at": st.get("last_assets_at"),
                "last_assets_error": st.get("last_assets_error"),
                "counts": {t: len(v) for t, v in self.list_backups().get("images", {}).items()},
            },
            "backup_dir": str(self.backup_dir),
            "disk": None if disk is None else {
                "total": disk.total, "used": disk.used, "free": disk.free,
            },
            "backups": {t: len(v) for t, v in self.list_backups().items()},
        }

    def _mailer(self):
        return Mailer({"email": self.email_cfg})

    def alert_failure(self, error):
        self._mailer().send(f"[AutoBrain Backup] {self.label()}: job failed",
                            f"A backup run for {self.label()} failed:\n\n{error}")

    def alert_corruption(self, error):
        self._mailer().send(f"[AutoBrain Backup] {self.label()}: corrupt backup detected",
                            f"A corrupted or invalid backup for {self.label()} was rejected:\n\n{error}")

    def alert_restore(self, status, name):
        self._mailer().send(f"[AutoBrain Backup] {self.label()}: restore completed",
                            f"Restore of {name} on {self.label()} completed with HTTP {status}.")
