# AutoBrain Backup

> Create a new Outline **collection** named **AutoBrain Backup** (parent: AutoBrain,
> or standalone), then add this document as the collection's index/README doc.

## Summary

`autobrain-backup` is the off-box backup service for AutoBrain. It connects
to an AutoBrain instance with the admin API key, downloads a full database
snapshot every hour, combines snapshots into hourly/daily/weekly retention
tiers, restores on demand, emails alerts on job failure and corruption, and
surfaces stats/health in a web GUI. Config lives in a file on the docker host
and is edited from the GUI.

Public repo: https://github.com/CannonFodder151/autobrain-backup
Public image: `ghcr.io/cannonfodder151/autobrain-backup` (`:latest`, `:1.0.0`)

## How it works

1. Every `schedule_interval` seconds (default 3600) the service fetches
   `GET {instance_url}/admin-api/backup` with `X-Admin-API-Key`.
2. The payload is validated before saving: it must be a genuine AutoBrain
   backup (`app=autobrain`, `kind=backup`, non-empty `data`). Anything else
   is rejected, counted as a failure, and triggers an alert email.
3. Snapshots are stored under `hourly/`, promoted to `daily/` once per day
   and `weekly/` once per week, then pruned to the configured retention
   (defaults: 24 hourly, 30 daily, 12 weekly). Because backups are full
   snapshots, "combining" means promoting the newest snapshot up a tier and
   pruning old ones.
4. Restore: pick a stored backup (or upload one) in the GUI. The service
   re-validates it, then POSTs it to `{instance_url}/admin-api/restore` as a
   multipart upload. Restore wipes existing data — the GUI requires typing
   `RESTORE` to confirm.
5. Alerts are sent via SMTP (STARTTLS) on job failure and on detected
   corruption, and after a completed restore.

### The agent companion

`autobrain-backup-agent` (public repo) is the counterpart that runs on the
AutoBrain host: it pulls the same snapshot and can POST it to this service's
`/ingest` endpoint (keyed with the configured ingest key). Both paths feed
the same storage and retention.

## Deployment

```yaml
services:
  autobrain-backup:
    image: ghcr.io/cannonfodder151/autobrain-backup:latest
    container_name: autobrain-backup
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - /srv/autobrain-backup/config:/config   # config file
      - /srv/autobrain-backup/data:/backups    # archives
```

Then open `http://<host>:8080`, enter the AutoBrain instance URL and its
admin API key, set SMTP settings, and save. First scheduled backup runs
after the first interval; `Run backup now` triggers immediately.

## Prerequisite

The AutoBrain instance must have `ADMIN_API_KEY` set (see
`docker-compose.hosted.yml` — `ADMIN_API_KEY: ${ADMIN_API_KEY:-}`) and must
run backend code that includes the `GET /admin-api/backup` and
`POST /admin-api/restore` endpoints (added in backend commit `4dac8893`,
AutoBrain repo). These endpoints are also used by `autobrain-backup-agent`.

## Configuration

Edited in the web GUI; persisted to `/config/autobrain-backup.json`:

| Setting | Purpose |
| --- | --- |
| `instance_url` | AutoBrain instance base URL |
| `api_key` | Admin API key (`X-Admin-API-Key`) |
| `backup_endpoint` | default `/admin-api/backup` |
| `restore_endpoint` | default `/admin-api/restore` |
| `schedule_interval` | seconds between runs, default 3600 |
| `retention.hourly/daily/weekly` | tier sizes, defaults 24/30/12 |
| `gui_key` | optional password for the web console |
| `ingest_key` | optional key for the agent `/ingest` push |
| `email` | SMTP host/port/user/password/from/to + STARTTLS toggle |

Secrets are masked in the API (`********`) and preserved when the GUI saves.

## Security notes

- Set a GUI key to protect the console.
- Config file on the host holds secrets in plaintext — restrict mount
  permissions (e.g. `chmod 700 /srv/autobrain-backup/config`).
- Restore is destructive; it is gated behind typed confirmation and a
  corruption check on the file before anything is sent.

## Architecture

- `server.py` — stdlib HTTP server (GUI + JSON API + `/ingest`) and the
  background scheduler thread.
- `engine.py` — fetch/validate/store, retention rotation, restore, SMTP
  alerts, config + state persistence.
- `static/index.html` — single-page web console (vanilla JS, no build step).
- `test_engine.py` — runnable self-check (`python3 test_engine.py`).

Deterministic, stdlib-only, no AI dependency. Image: `python:3.12-alpine`,
runs as non-root.

## API surface

`GET /` (GUI), `GET /api/status`, `GET/POST /api/config`,
`POST /api/backup/run`, `GET /api/backups`,
`GET /api/backup/download?name=…`, `POST /api/backup/restore`,
`DELETE /api/backup/delete?name=…`, `POST /ingest`.

## Status

- v1.0.0 built, tested (all engine checks green), image published to GHCR.
- Docker Hub image (`cannonfodder151/autobrain-backup`) auto-publishes on
  `v*` tags via GitHub Actions; pending a refreshed Docker Hub token.
- Backend endpoints merged to AutoBrain `main` (commit `4dac8893`); instances
  need a redeploy + `ADMIN_API_KEY` to expose them.
