# Changelog

All notable changes to `autobrain-backup` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [3.0.4] - 2026-09-03

### Fixed (AUT-2310)
- `server.py` (`_scheduler_step`): when `consecutive_failures` first crosses the
  alert threshold, stamp an `alerted_at` marker and skip re-firing the alert
  email on every subsequent tick. Without this, with the 60s retry window from
  AUT-2285 the alert spammed admins (and Discord, via the same mailer) once per
  minute after the threshold. The marker is cleared on the next successful run,
  so a fresh outage still alerts.
- `engine.py` (`State.FIELDS`): add `alerted_at` so it persists across restarts.
- `engine.py` (`run_backup`, `ingest`): clear `alerted_at` on success so a
  future outage will alert again.

### Fixed (AUT-2285)
- `server.py` (`_scheduler_step`): due-ness is now the OR of two clocks.
  Normal cadence: `last_run + schedule_interval`. Failure cadence:
  `last_attempt_at + 60s`. Previously only the first clock existed, so a
  single transient failure on a long schedule (1h on hosted) silenced
  retries for the rest of the interval.
- `engine.py` (`State.FIELDS`): add `last_attempt_at` so it persists; stamp
  it on every attempt, clear it (effectively, by overwriting) on success.
- The 3-strike alert path is preserved; `last_run` is stamped on the alert
  threshold to back off after a sustained outage.

## [3.0.3] - 2026-09-04

### Fixed (AUT-2370)
- `server.py` (`_scheduler_step`): after the 3-strike threshold alert fires,
  reset `consecutive_failures` to 0. Previously the counter stayed at 3+
  forever, so every subsequent tick that hit the threshold again immediately
  re-sent the alert email — a host that stayed down flooded the admin inbox
  with one email per `schedule_interval` (1h on hosted).
- `server.py` (`run_backup_now`): manual "Run Now" clicks now apply the
  same 3-strike threshold as the scheduler. Previously every manual
  failure immediately emailed the admin — clicking during deploy churn
  spammed the inbox.
- `server.py` (`run_backup_now`): reset `consecutive_failures` on a
  successful manual run so a stale counter from a prior down period can't
  immediately re-alert once the host recovers.
- `server.py` (`_scheduler_step`): reset `consecutive_failures` to 0 on a
  successful scheduled run (same reason).

## [3.0.2] - 2026-08-18

### Fixed (AUT-1023)
- `server.py` (`_scheduler_step`): transient backup failures no longer
  consume `last_run` so the next 60s tick retries instead of waiting a
  full `schedule_interval`. Alert only after 3 consecutive failures.
