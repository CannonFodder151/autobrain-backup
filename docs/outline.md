# AutoBrain Backup

> Create a new Outline **collection** named **AutoBrain Backup** (parent: AutoBrain,
> or standalone), then add this document as the collection's index/README doc.

## Summary

`autobrain-backup` is the off-box backup service for AutoBrain. It is
**multi-tenant**: the main page lists every AutoBrain instance being backed
up (each with an optional nickname). For each instance it downloads a full
database snapshot every hour (with the admin API key) plus a gzipped tar
archive of the instance's MinIO image assets, combines both into
hourly/daily/weekly retention tiers, restores either on demand, emails
alerts on job failure and corruption, and surfaces per-instance
stats/health in a web GUI. A **Test email** button verifies the SMTP alert
config. Config lives in a file on the docker host and is edited from the
GUI.

Public repo: https://github.com/CannonFodder151/autobrain-backup
Public image: `ghcr.io/cannonfodder151/autobrain-backup` (`:latest`, `:3.0.0`)

## How it works

1. **Instances** — the config holds a list of AutoBrain instances. Each has
   a nickname, admin key, retention, schedule and its own backup folder
   (`{backup_dir}/{instance_id}`). Add / rename / delete / pause from the GUI.
2. Every `schedule_interval` seconds (default 3600) the service fetches
   `GET {instance_url}/api/v1/admin-api/backup` with `X-Admin-API-Key`, plus
   the image archive `GET {instance_url}/api/v1/admin-api/assets/backup` (a
   tar.gz of every object in the instance's MinIO bucket, same run stamp).
3. The payload is validated before saving: the DB snapshot must be a genuine
   AutoBrain backup (`app=autobrain`, `kind=backup`, non-empty `data`); the
   image archive must be a readable gzip tar with safe member names. Anything
   else is rejected, counted as a failure, and triggers an alert email.
4. Snapshots are stored under `hourly/`, promoted to `daily/` once per day
   and `weekly/` once per week, then pruned to the configured retention
   (defaults: 24 hourly, 30 daily, 12 weekly). Image archives follow the same
   stamp into a per-tier `images/` folder with identical promotion + pruning.
5. Restore: pick a stored backup or image archive (or upload one) in the GUI.
   The service re-validates it, then POSTs it to the instance's
   `/api/v1/admin-api/restore` (DB) or `/api/v1/admin-api/assets/restore`
   (images) as a multipart upload. Restore wipes existing data — the GUI
   requires typing `RESTORE` to confirm for both.
6. Alerts are sent via SMTP (STARTTLS) on job failure, on detected
   corruption, and after a completed restore. SMTP settings mirror the
   AutoBrain app (host, port, TLS, user, password, from-address). A
   **Test email** button sends a test alert and reports success or the exact
   error.

### The agent companion

`autobrain-backup-agent` (public repo) is the counterpart that runs on the
AutoBrain host: it pulls the same snapshot and can POST it to this service's
`/ingest?instance=<id>` endpoint (keyed with the instance's ingest key). Both
paths feed the same storage and retention.

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

Then open `http://<host>:8080`, press **Add instance**, enter the instance
URL, admin API key and nickname, set SMTP settings, and save. Press **Test
email** to verify alerts. First scheduled backup runs after the first
interval; `Run backup now` triggers immediately.

## Hosted instance (AutoBrain-Hosted)

The hosted AutoBrain production instance is backed up by the same service:

- Instance URL: `https://hosted.autobrainservice.app`
- Backup endpoint: `/api/v1/admin-api/backup`, restore: `/api/v1/admin-api/restore`
- SMTP mirrors the hosted stack: `mail-au.smtp2go.com:587`, STARTTLS,
  user `autobrainservice.app`, from `no-reply@autobrainservice.app`.

## Prerequisite

The AutoBrain instance must have `ADMIN_API_KEY` set (see
`docker-compose.hosted.yml` — `ADMIN_API_KEY: ${ADMIN_API_KEY:-}`) and must
run backend code that includes `GET /admin-api/backup` and
`POST /admin-api/restore` (mounted under the API v1 prefix, i.e.
`/api/v1/admin-api/backup`, added in backend commit `4dac8893`, AutoBrain
repo) plus `GET /admin-api/assets/backup` and `POST /admin-api/assets/restore`
(since backend `3.x`, for MinIO image assets). The assets endpoints need
MinIO configured (default in the docker-compose stack). Instances on an older
backend still back up their DB; image archives are skipped with a status
warning. These endpoints are also used by `autobrain-backup-agent`.

## Configuration

Edited in the web GUI; persisted to `/config/autobrain-backup.json` (v2).
A v1 single-instance config migrates automatically into one instance entry.

Global settings:

| Setting | Purpose |
| --- | --- |
| `gui_key` / `gui_user` / `gui_password` | optional web-console password / login |
| `backup_dir` | base folder; each instance gets `{backup_dir}/{id}` |
| `email` | SMTP host/port/user/password/from/to + STARTTLS toggle (shared) |

Per-instance settings:

| Setting | Purpose |
| --- | --- |
| `nickname` | optional label shown in the instances list |
| `instance_url` | AutoBrain instance base URL |
| `api_key` | Admin API key (`X-Admin-API-Key`) |
| `backup_endpoint` | default `/api/v1/admin-api/backup` |
| `restore_endpoint` | default `/api/v1/admin-api/restore` |
| `assets_backup_endpoint` | default `/api/v1/admin-api/assets/backup` |
| `assets_restore_endpoint` | default `/api/v1/admin-api/assets/restore` |
| `schedule_interval` | seconds between runs, default 3600 |
| `retention.hourly/daily/weekly` | tier sizes, defaults 24/30/12 |
| `ingest_key` | optional key for the agent `/ingest` push |
| `enabled` | false pauses scheduled backups |
| `backup_dir` | optional override of this instance's archive folder |

Secrets are masked in the API (`********`) and preserved when the GUI saves.

## Security notes

- Set a GUI key / login to protect the console.
- Config file on the host holds secrets in plaintext — restrict mount
  permissions (e.g. `chmod 700 /srv/autobrain-backup/config`).
- Restore is destructive; it is gated behind typed confirmation and a
  corruption check on the file before anything is sent.

## Architecture

- `server.py` — stdlib HTTP server (GUI + JSON API + `/ingest`) and
  per-instance background scheduler threads.
- `engine.py` — multi-tenant config/state, fetch/validate/store, retention
  rotation, restore, SMTP alerts.
- `static/index.html` — single-page web console (vanilla JS, no build step).
- `test_engine.py` — runnable self-check (`python3 test_engine.py`).

Deterministic, stdlib-only, no AI dependency. Image: `python:3.12-alpine`,
runs as non-root.

## API surface

`GET /` (GUI), `GET /api/instances`, `POST /api/instances`,
`POST /api/instances/update`, `DELETE /api/instances?instance=<id>`,
`GET /api/status?instance=<id>`, `GET/POST /api/config?instance=<id>`,
`POST /api/email/test`, `POST /api/backup/run?instance=<id>`,
`GET /api/backups?instance=<id>`,
`GET /api/backup/download?instance=<id>&name=…`,
`POST /api/backup/restore?instance=<id>`,
`DELETE /api/backup/delete?instance=<id>&name=…`,
`GET /api/assets/download?instance=<id>&name=…`,
`POST /api/assets/restore?instance=<id>`,
`DELETE /api/assets/delete?instance=<id>&name=…`, `POST /ingest?instance=<id>`.
With one instance configured, the `?instance=` parameter is optional.

## Status

- v3.0.0: image (MinIO) backup + restore. DB snapshot and image archive
  fetched together, stored under per-tier `images/`, listed/downloaded/
  restored via GUI + API, restore validated + `RESTORE`-confirmed. All
  engine + API checks green.
- v2.0.0: multi-tenant (instances list + nicknames), per-instance folders +
  state, hosted instance wired in, SMTP alerts mirror the AutoBrain app,
  test-email button.
- v1.1.0: username/password login GUI.
- v1.0.0: initial single-instance service; GHCR + Docker Hub images publish
  on `v*` tags via GitHub Actions.
- Backend assets endpoints merged to AutoBrain `main` (PR #20); instances
  need a redeploy + `ADMIN_API_KEY` to expose them.
