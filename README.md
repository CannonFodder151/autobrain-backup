# AutoBrain Backup

A web-interface backup tool for an AutoBrain instance. It downloads a full
database snapshot on a schedule (hourly by default), combines snapshots into
hourly/daily/weekly retention tiers, provides a one-click restore, shows
stats and health in a web GUI, and emails alerts when a backup job fails or a
corrupt backup is detected.

Deterministic and stdlib-only — no AI, no external services. Backups are full
database snapshots (validated as genuine AutoBrain backups before anything is
saved or restored).

## What it does

1. **Backup** — every `schedule_interval` seconds (default 3600) fetches
   `GET {instance_url}/admin-api/backup` with `X-Admin-API-Key`.
2. **Validation** — every payload must be a real AutoBrain backup
   (`app=autobrain`, `kind=backup`, non-empty `data`). Invalid payloads are
   rejected, counted as failures, and alert email is sent.
3. **Retention tiers** — snapshots are combined into
   `hourly/` (one per run, keep N), `daily/` (one per day, keep N) and
   `weekly/` (one per week, keep N). Since backups are full snapshots,
   "combining" means promoting the newest snapshot up a tier and pruning old
   ones to the configured limits.
4. **Restore** — pick a stored backup (or upload one) in the GUI; the service
   re-validates it, then POSTs it to `{instance_url}/admin-api/restore` as a
   multipart upload. Restoring wipes existing data on the instance — the GUI
   requires typing `RESTORE` to confirm.
5. **Alerts** — SMTP email on backup job failure and on detected corruption.
6. **Stats & health** — the GUI shows last backup, status, error, next run,
   counters, retention counts, and disk usage.

An `autobrain-backup-agent` (see the `autobrain-backup-agent` repo) can push
snapshots in instead of the service pulling: it POSTs the backup file to the
service's `POST /ingest` endpoint (optionally keyed with the configured
ingest key). Both modes feed the same storage and retention.

## Quick start (Docker)

All settings live in a config file on the docker host (mounted folder),
editable from the web GUI — no container rebuild or SSH editing needed.

```yaml
services:
  autobrain-backup:
    image: cannonfodder151/autobrain-backup:latest
    container_name: autobrain-backup
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - /srv/autobrain-backup/config:/config     # config file lives here
      - /srv/autobrain-backup/data:/backups      # backup archives live here
```

Then open `http://<host>:8080`, enter the AutoBrain instance URL and its
admin API key (`X-Admin-API-Key`), set SMTP settings for alerts, and hit
**Save configuration**. The first scheduled backup runs shortly after the
first interval. Press **Run backup now** to trigger one immediately.

### Security

- Set a **GUI key** in the config to password-protect the web console
  (prompted on load; passed as `X-Gui-Key`).
- If you set an **ingest key**, only agents that present it can push backups
  to `POST /ingest`.
- The config file on the host contains secrets in plaintext — restrict access
  to the mount folder (e.g. `chmod 700 /srv/autobrain-backup/config`).

## Architecture

| File | Purpose |
| --- | --- |
| `server.py` | stdlib HTTP server: GUI + JSON API + `POST /ingest`, background scheduler thread |
| `engine.py` | backup fetch/validate/store, retention rotation, restore, SMTP alerts, config & state persistence |
| `static/index.html` | single-page web console (vanilla JS, no build step) |
| `test_engine.py` | runnable self-check (`python3 test_engine.py`) |

### API

| Method & path | Purpose |
| --- | --- |
| `GET /` | web console |
| `GET /api/status` | stats & health |
| `GET /api/config` | current config (secrets masked) |
| `POST /api/config` | save config |
| `POST /api/backup/run` | trigger a backup now |
| `GET /api/backups` | list hourly/daily/weekly backups |
| `GET /api/backup/download?name=…` | download a stored backup |
| `POST /api/backup/restore` | restore (body `{name|content_b64, confirm:true}`) |
| `DELETE /api/backup/delete?name=…` | delete a stored backup |
| `POST /ingest` | receive a backup pushed by autobrain-backup-agent |

## Config file

The GUI edits `/config/autobrain-backup.json` on the host:

```json
{
  "instance_url": "https://app.autobrainservice.app",
  "api_key": "your-admin-api-key",
  "backup_endpoint": "/admin-api/backup",
  "restore_endpoint": "/admin-api/restore",
  "backup_dir": "/backups",
  "schedule_interval": 3600,
  "gui_key": "",
  "ingest_key": "",
  "retention": {"hourly": 24, "daily": 30, "weekly": 12},
  "email": {
    "enabled": false,
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "use_tls": true,
    "from": "",
    "to": []
  }
}
```

## Prerequisite

`GET /admin-api/backup` and `POST /admin-api/restore` must be enabled on the
AutoBrain instance (they are available since backend `2.x`; both require
`ADMIN_API_KEY` to be set on the instance).

## Test

```bash
python3 test_engine.py
```

## License

MIT — see [LICENSE](LICENSE).
