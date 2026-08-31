# aiduMEI Operations

## Scheduled jobs

Use `scripts/update_crontab.sh --list` to inspect the intended maintenance set. Install with `scripts/update_crontab.sh`.

| Job | Frequency | Command | Output |
|---|---|---|---|
| Health inspection | 5 minutes | `python scripts/health_check.py` | stdout/monitor |
| Memory consolidation | daily | `python scripts/consolidator.py` | service logs and DB updates |
| Backup create | daily | `bash scripts/backup_gate.sh create daily-$(date +%F)` | persistent backup directory |
| Backup verify | weekly | `bash scripts/backup_gate.sh verify <backup_dir>` | verification report |
| Pre-upgrade gate | before upgrade | `bash scripts/pre-upgrade-check.sh` | readiness report |
| Post-upgrade check | after upgrade | `bash scripts/post-upgrade-check.sh` | validation report |
| E2E smoke | after upgrade/restore | `python scripts/e2e_smoke.py --json` | PASS/WARN/FAIL report |
| Log rotation | daily | deployment logrotate policy | rotated logs |
| SQLite checkpoint/vacuum | scheduled by DB policy | SQLite maintenance procedure | compact DB files |

## Operational order

1. Take a verified backup.
2. Run the pre-upgrade gate.
3. Stop or drain writes if the upgrade requires exclusive schema access.
4. Upgrade exact commit and dependencies.
5. Restart and run `/health`.
6. Run `scripts/e2e_smoke.py --json`.
7. Run the post-upgrade check.
8. Record commit, test numbers, and rollback point.

## Recovery principle

A backup is not complete until it is verified and restored into a test path, followed by e2e smoke success.
