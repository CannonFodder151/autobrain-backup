# Changelog

## [Unreleased]

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
- Bump `VERSION` to 3.0.3.

## [3.0.2] - 2026-08-18

### Fixed (AUT-1023)
- `server.py` (`_scheduler_step`): transient backup failures no longer
  consume `last_run` so the next 60s tick retries instead of waiting a
  full `schedule_interval`. Alert only after 3 consecutive failures.