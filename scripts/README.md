# scripts/ Index

| Script | Purpose | Mutates data? | Repeatable? | When to run |
|---|---|---|---|---|
| `connect_host.py` | One-command host wiring (Claude Code / Codex / Hermes): back up host config, idempotently add read/write/distill hooks, then a live nonce self-test through the real hooks; first line `服务 ✅/❌ · 宿主接入 ✅/❌` | Host config (backed up); one test memory, deleted after | Idempotent; `--dry-run` / `--uninstall` / `--check` | After install, and after moving the repo or changing the service URL |
| `consolidate.py` | f0.8+ nightly consolidation by hand: `run` groups memories about the same subject and marks older versions superseded (exact duplicates by rule, changed state by the configured LLM on near-neighbour clusters), `report` shows link/run counts, `restore ID --user U` un-supersedes one memory; `--data-dir` works on a copy | `run` writes link rows only (facts.db `consolidation_links`, never memory text); `--mode on` also writes lineage | Incremental by watermark; `--full` re-scans | The service runs it nightly when `AIDUMEI_CONSOLIDATE_MODE` is shadow/on; by hand to preview |
| `f08p_data_cleanup.py` | f0.8+ ④ cleanup: `wash` (principle-7 redact reflections / memory_edits / autodream_log, backup first), `reasoning` (list stored reasoning-text summaries; delete via the service's `/delete`), `repack` (redact old backups in place and re-pack, never delete; skips recent / `--keep`; leaves config secrets alone), `transcripts` (CC `*.jsonl` retention + redaction, api-request bodies stubbed) | Only with `--apply` | Idempotent; dry run is the default | At deploy time, then transcripts weekly |
| `cc_memory_sweep.py` | f0.8: daily backstop over every CC memory dir: same redact + import as the `PostToolUse` bridge hook (`integrations/claude-code/aidumem-cc-memory-bridge.sh` → `cc_memory_bridge.py`, installed by `connect_host.py`); `--trim` stubs imported topic files older than `--days` (14) and caps `MEMORY.md` at `--index-cap` (80) lines, dropping only entries whose file is already imported; originals backed up to `~/.aidumem/cc-bridge-trash/` | Only with `--apply` (redact/import) and `--apply --trim` (stubs, index cap) | Idempotent; `--dry-run` is the default | Daily on the machine where Claude Code runs |
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
| `decision_compare.py` | Same-corpus Drex/Jev/Clef typed-decision comparison; credentials only from environment | No | Yes | Before selecting a decision provider |
| `release_scan.py` | Release-sensitive scan | No | Yes | Release gate |
| `push_gate.sh` | Full push gate | No | Yes | Before pushing repositories |
| `phase4a_test.sh` | Legacy phase test | No | Yes | Historical diagnostics |
