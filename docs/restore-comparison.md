# Restore Tool Comparison

| Tool | Purpose | Writes data? | Repeatable? | When to use |
|---|---|---:|---:|---|
| `scripts/restore_backup.py` | Replay Qdrant snapshot points through the live API | Yes | Replays each point | Restore vector points from an explicit `storage.sqlite` snapshot |
| `scripts/restore_from_facts.py` | Rebuild vector memories from facts rows | Yes | Replays facts | Fallback when a vector snapshot is unavailable but facts are intact |
| `scripts/restore_bg.py` | Background restore variant | Yes | Replays records | Long-running restore jobs with service supervision |
| `scripts/restore_gate.sh` | Verify a backup and prove recovery | Dry-run: no; apply: yes | Yes | Gate every restore with health + e2e validation |

## Selection rules

1. Prefer a full verified backup with `restore_gate.sh`.
2. Use `restore_backup.py` only with an explicit snapshot path.
3. Use `restore_from_facts.py` only when facts are known-good and vector snapshots are missing.
4. Treat a restore as incomplete until health and `scripts/e2e_smoke.py` both pass.

## Safety

- Never let a restore tool guess a “likely” backup.
- Never restore directly into a live data directory without a tested rollback point.
- Always run `restore_gate.sh --dry-run` before apply mode.
- Apply mode is disabled unless `RESTORE_GATE_ALLOW_APPLY=1` is explicitly set.
