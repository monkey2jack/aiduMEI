# Backup, Restore, Upgrade, Rollback

## Backup

```bash
bash scripts/backup_gate.sh create upgrade-$(date +%F)
bash scripts/backup_gate.sh verify <backup_dir>
```

A valid backup has checksums, SQLite `quick_check`, and a verification marker. Store backups outside `/tmp` and outside the repository working tree.

## Restore

1. Stop or drain writes if the backend cannot safely restore concurrently.
2. Restore the exact verified snapshot.
3. Run `python scripts/e2e_smoke.py --json`.
4. Do not declare recovery success until smoke exits 0.

Vector snapshot restore:

```bash
python scripts/restore_backup.py <storage.sqlite> --dry-run
python scripts/restore_backup.py <storage.sqlite>
```

The script requires an explicit snapshot path; it never guesses a historical backup.

## Upgrade

```bash
bash scripts/pre-upgrade-check.sh
bash scripts/backup_gate.sh create upgrade-$(date +%F)
git fetch origin && git reset --hard <exact-commit>
pip install -r requirements.txt
systemctl restart aidumem-api
python scripts/e2e_smoke.py --json
bash scripts/post-upgrade-check.sh
```

## Rollback

1. Stop the service.
2. Restore code to the recorded exact commit.
3. Restore data from the verified pre-upgrade backup.
4. Restart and run e2e smoke.
5. Record what failed and why rollback was needed.
