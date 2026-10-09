# Backup, Restore, Upgrade, Rollback

## Backup

```bash
bash scripts/backup_gate.sh create upgrade-$(date +%F)
bash scripts/backup_gate.sh verify <backup_dir>
```

Keep backups outside `/tmp` and outside the repository. The backup includes an
exact typed member manifest, checksums, SQLite online snapshots and durable
business WAL. Verification rejects missing or extra files, links, ambiguous
paths and corrupt SQLite. It preserves the originals while checking copies.
Keep `mutation_journal.sqlite3` and `mutation_journal.identity.json` together.
Neither a marker file alone nor a successful API response proves a valid backup.

Per-file online snapshots are not an atomic cross-store snapshot. Record the
acquisition window and reconcile concurrent writes. A running Qdrant server
needs snapshots of each active collection through its own snapshot API; copying
historical local `storage.sqlite` files does not back up that server.

## Isolated restore and historical readback

```bash
bash scripts/restore_gate.sh --dry-run <backup_dir>
# The parent must exist; the target itself must not exist.
RESTORE_GATE_ALLOW_APPLY=1 AIDUMEM_DATA_DIR=/path/to/new-isolated-data bash scripts/restore_gate.sh --isolated <backup_dir>
bash scripts/restore_gate.sh --drill /path/to/new-isolated-data <snapshot_id-from-restore-receipt> /path/to/historical-fixture.json
```

Apply holds an exclusive process lock and refuses existing targets, including
live data directories. New incomplete targets are retained for inspection.
It does not start a service or contact an API. The separate drill checks the
restored directory identity, exact snapshot and an existing historical SQLite
row in a managed child process. Its fixture has these fields:

```json
{"database":"facts.db","table":"facts","key_column":"id","key":123,
 "value_column":"fact_value","expected_sha256":"<SHA256-of-the-original-UTF8-value>"}
```

Choose a real, unique row and freeze its expected hash before restoration.
This proves local historical readback only. Verify Qdrant snapshots by restoring
them into a separate empty server and checking historical points. Verify the
restored application separately with the correct configuration, data paths,
dependencies and service identity; an unrelated healthy endpoint is no proof.

The older `scripts/restore_backup.py <storage.sqlite> --dry-run` previews replay
of a local Qdrant snapshot through the configured API. Its apply mode writes
records; it is not a whole-system recovery procedure or an online rollback tool.

## Upgrade and rollback

Before deployment, verify the exact candidate in isolation, record immediate
backups and restore evidence, preserve configuration/hooks and confirm service
account permissions. Follow [f0.4 operations](F04_UPGRADE.md) for journal and WAL
compatibility, single-process ownership and repair boundaries.

A code rollback restores the recorded code, hooks and only its own configuration
changes. **Do not overlay old backup data onto normal writes received since the
snapshot.** Inspect unresolved journal/WAL work and validate schema compatibility
before starting an older reader. WAL conversion alone does not certify all
application schemas. Record each recovery step and verify the intended service,
historical records, new writes and all previously active maintenance jobs.

Data recovery is a separate, explicitly scoped operation after diagnosis; it
must not be an automatic side effect of a failed code deployment.
