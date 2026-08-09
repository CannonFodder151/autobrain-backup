# AutoBrain Backup

A web-interface backup tool for **one or more AutoBrain instances**. The main
page lists every backup instance (each with an optional nickname); you add an
AutoBrain instance, it downloads a full database snapshot on a schedule
(hourly by default), combines snapshots into hourly/daily/weekly retention
tiers, provides a one-click restore, shows stats and health per instance in a
web GUI, and emails alerts when a backup job fails or a corrupt backup is
detected. A **test email** button verifies the SMTP alert config.

Deterministic and stdlib-only — no AI, no external services. Backups are full
database snapshots (validated as genuine AutoBrain backups before anything is
saved or restored).

## What it does

1. **Multi-tenant** — the config holds a list of AutoBrain instances. Each has
   a nickname, its own admin key, retention and schedule, and its own backup
   folder (`{backup_dir}/{instance_id}`). Instances can be added, renamed,
   deleted and paused from the GUI.
2. **Backup** — every `schedule_interval` seconds (default 3600) fetches
   `GET {instance_url}/api/v1/admin-api/backup` with `X-Admin-API-Key`.
3. **Validation** — every payload must be a real AutoBrain backup
   (`app=autobrain`, `kind=backup`, non-empty `data`). Invalid payloads are
   rejected, counted as failures, and an alert email is sent.
4. **Retention tiers** — snapshots are combined into
   `hourly/` (one per run, keep N), `daily/` (one per day, keep N) and
   `weekly/` (one per week, keep N). Since backups are full snapshots,
   "combining" means promoting the newest snapshot up a tier and pruning old
   ones to the configured limits.
5. **Restore** — pick a stored backup (or upload one) in the GUI; the service
   re-validates it, then POSTs it to `{instance_url}/api/v1/admin-api/restore`
   as a multipart upload. Restoring wipes existing data on the instance — the
   GUI requires typing `RESTORE` to confirm.
6. **Alerts** — SMTP email on backup job failure and on detected corruption.
   The SMTP settings mirror the AutoBrain app (host, port, TLS, user,
   password, from-address). A **Test email** button sends a test alert and
   reports success or the exact error.
7. **Stats & health** — the GUI shows last backup, status, error, next run,
   counters, retention counts, and disk usage per instance.

An `autobrain-backup-agent` (see the `autobrain-backup-agent` repo) can push
snapshots in instead of the service pulling: it POSTs the backup file to the
service's `POST /ingest?instance=<id>` endpoint (optionally keyed with the
instance's ingest key). Both modes feed the same storage and retention.

## Quick start (Docker)

All settings live in a config file on the docker host (mounted folder),
editable from the web GUI — no container rebuild or SSH editing needed.

```yaml
services:
  autobrain-backup:
    image: ghcr.io/cannonfodder151/autobrain-backup:latest
    container_name: autobrain-backup
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - /srv/autobrain-backup/config:/config     # config file lives here
      - /srv/autobrain-backup/data:/backups      # backup archives live here
```

Images are published to GHCR (`ghcr.io/cannonfodder151/autobrain-backup`).
A `cannonfodder151/autobrain-backup` Docker Hub image is published on every
`v*` tag via GitHub Actions.

Then open `http://<host>:8080`, press **Add instance**, enter the AutoBrain
instance URL, its admin API key and an optional nickname, then set SMTP
settings for alerts and hit **Save configuration**. Press **Test email** to
verify alerts. The first scheduled backup runs shortly after the first
interval. Press **Run backup now** to trigger one immediately.

### Security

- Set a **GUI key / username+password** in the config to password-protect the
  web console (prompted on load; `X-Gui-Key` header also supported).
- Each instance can have its own **ingest key** — only agents that present it
  can push backups to that instance's `POST /ingest`.
- The config file on the host contains secrets in plaintext — restrict access
  to the mount folder (e.g. `chmod 700 /srv/autobrain-backup/config`).

## Architecture

| File | Purpose |
| --- | --- |
| `server.py` | stdlib HTTP server: GUI + JSON API + `POST /ingest`, per-instance background schedulers |
| `engine.py` | multi-tenant config & state, backup fetch/validate/store, retention rotation, restore, SMTP alerts |
| `static/index.html` | single-page web console (vanilla JS, no build step) |
| `test_engine.py` | runnable self-check (`python3 test_engine.py`) |

### API

| Method & path | Purpose |
| --- | --- |
| `GET /` | web console |
| `GET /api/instances` | list instances (nickname, url, health) |
| `POST /api/instances` | create an instance |
| `POST /api/instances/update` | rename / edit an instance (`{id, …}`) |
| `DELETE /api/instances?instance=<id>` | delete an instance (archives kept on disk) |
| `GET /api/status?instance=<id>` | stats & health for an instance |
| `GET /api/config?instance=<id>` | global config + selected instance (secrets masked) |
| `POST /api/config` | save instance + email/gui settings |
| `POST /api/email/test` | send a test alert email; reports success/failure |
| `POST /api/backup/run?instance=<id>` | trigger a backup now |
| `GET /api/backups?instance=<id>` | list hourly/daily/weekly backups |
| `GET /api/backup/download?instance=<id>&name=…` | download a stored backup |
| `POST /api/backup/restore?instance=<id>` | restore (body `{name|content_b64, confirm:true}`) |
| `DELETE /api/backup/delete?instance=<id>&name=…` | delete a stored backup |
| `POST /ingest?instance=<id>` | receive a backup pushed by autobrain-backup-agent |

With exactly one instance configured, the `?instance=` parameter is optional
(single-instance clients keep working).

## Config file

The GUI edits `/config/autobrain-backup.json` on the host. A v1
single-instance config is migrated automatically into an instance entry.

```json
{
  "version": 2,
  "gui_key": "",
  "gui_user": "",
  "gui_password": "",
  "backup_dir": "/backups",
  "email": {
    "enabled": true,
    "smtp_host": "mail-au.smtp2go.com",
    "smtp_port": 587,
    "smtp_user": "autobrainservice.app",
    "smtp_password": "",
    "use_tls": true,
    "from": "no-reply@autobrainservice.app",
    "to": ["alerts@example.com"]
  },
  "instances": [
    {
      "id": "inst_abc123",
      "nickname": "Hosted production",
      "instance_url": "https://hosted.autobrainservice.app",
      "api_key": "",
      "backup_endpoint": "/api/v1/admin-api/backup",
      "restore_endpoint": "/api/v1/admin-api/restore",
      "schedule_interval": 3600,
      "ingest_key": "",
      "retention": {"hourly": 24, "daily": 30, "weekly": 12},
      "enabled": true,
      "backup_dir": ""
    }
  ]
}
```

Each instance's archives live in `{backup_dir}/{instance_id}/` (override with
the instance's `backup_dir`), with its run state in `state.json` alongside.

## Prerequisite

`GET /api/v1/admin-api/backup` and `POST /api/v1/admin-api/restore` must be
enabled on the AutoBrain instance (they are available since backend `2.x`;
both require `ADMIN_API_KEY` to be set on the instance).

## Test

```bash
python3 test_engine.py
```

## License

MIT — see [LICENSE](LICENSE).
