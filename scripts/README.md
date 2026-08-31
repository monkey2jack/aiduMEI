# scripts/ Index

| Script | Purpose | Mutates data? | Repeatable? | When to run |
|---|---|---|---|---|
| `e2e_smoke.py` | End-to-end write/recall/trace/cleanup acceptance via HTTP | Yes, in a temporary tenant | Yes | After install, upgrade, restore, or host integration |
| `health_check.py` | Layer/API/model health report | No | Yes | Health inspection |
| `update_crontab.sh` | Install or list periodic maintenance jobs | Changes crontab | Idempotent | Once per deployment; use `--list` to inspect |
| `consolidator.py` | Memory consolidation/refinement job | Yes | Yes | Scheduled maintenance |
| `backup_gate.sh` | Create/verify backups and enforce upgrade gate | Creates backup files | Yes | Before upgrades and as scheduled backups |
| `pre-upgrade-check.sh` | Upgrade gate and readiness checks | No | Yes | Before upgrade |
| `post-upgrade-check.sh` | Post-upgrade validation | No | Yes | After upgrade |
| `restore_backup.py` | Replay Qdrant snapshot through live API | Yes | Replays points | Recovery, after backup verification |
| `restore_from_facts.py` | Rebuild vector memories from facts DB | Yes | Replays facts | Facts-based recovery only |
| `restore_bg.py` | Background restore variant | Yes | Replays points | Long-running recovery |
| `backfill_local_vectors.py` | Backfill local vector index | Yes | Idempotent upsert | After switching `cloud` to `auto/local` |
| `backfill_core_vectors.py` | Backfill core memory vectors | Yes | Idempotent upsert | After core-memory migration |
| `backfill_tiers.py` | Backfill tiered memories | Yes | Idempotent upsert | After tier migration |
| `fetch_local_embed_model.py` | Download and stage local embedding model | Writes model cache | Yes | Deployment for auto/local |
| `deploy_manifest.py` | Build deployment manifest | No | Yes | Packaging/deployment audit |
| `vector_shadow_poc.py` | Vector backend shadow checks | No | Yes | Backend contract diagnostics |
| `release_scan.py` | Release-sensitive scan | No | Yes | Release gate |
| `push_gate.sh` | Full push gate | No | Yes | Before pushing repositories |
| `phase4a_test.sh` | Legacy phase test | No | Yes | Historical diagnostics |
