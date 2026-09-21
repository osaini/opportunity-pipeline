# Operations runbook

## Service targets

- API availability target: 99.5% monthly for invited testers.
- Read API p95 target: 750 ms; write API p95 target: 1.5 s.
- Queue age target: under 10 minutes. `backpressure=true`, any dead-letter job, 5xx rate above 2%, or failed restore drill is alert-worthy.

## Deploy and rollback

1. Create an encrypted backup and perform `python -m opportunity_app.ops_cli drill BACKUP`.
2. Apply the migration to a staging copy and run the parity suite.
3. Deploy API, then worker, with public signup disabled by feature flag.
4. Check `/api/v1/health`, admin source health, queue depth, and critical journeys.
5. Roll back the image if checks fail. Restore into a new database path/instance; never overwrite the only database copy.

## Failure drills

- Provider outage: disconnect/disable its source control; monitoring remains preview-only and notifications remain queued/suppressed.
- Queue backlog: stop producers with a feature flag, add worker capacity, retry dead jobs only after fixing the cause.
- Model outage: deterministic ranking and evidence views continue; agent abstains and records failure without mutation.
- Notification failure: preserve the deduplicated outbox, disable the affected channel, and keep in-app delivery.
- Suspected breach: rotate all three API tokens/provider secrets, disconnect provider accounts, preserve operational audit, and notify affected testers.
- Data loss: restore the latest encrypted backup into an isolated destination, run integrity/parity tests, then switch traffic.

## Backup commands

Set `PIPELINE_BACKUP_KEY` to a Fernet key kept outside the repository.

```powershell
py -3 -m opportunity_app.ops_cli backup backups/platform-2026-08-10.enc
py -3 -m opportunity_app.ops_cli drill backups/platform-2026-08-10.enc
py -3 -m opportunity_app.ops_cli restore backups/platform-2026-08-10.enc data/restored-platform.db
```

For PostgreSQL, pass the database URL to `backup --db`. A restore drill must use
an isolated disposable database via `drill BACKUP --target postgresql://...`;
`pg_restore --clean` deliberately replaces objects in that explicit target.
