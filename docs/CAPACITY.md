# aiduMEI Capacity Guide

aiduMEI exposes deployment capacity through `/health` and `scripts/report.py`. Use this document to interpret those fields.

## Engine modes

| Resource | `cloud` | `auto` | `local` |
|---|---:|---:|---:|
| Baseline RSS | ~280 MB | ~430 MB | ~430 MB |
| Local model cache | none | ~91 MB | ~91 MB |
| External model calls | cloud only | cloud first, local fallback | none |
| Recommended minimum | 1 vCPU / 1 GB | 2 vCPU / 2 GB | 2 vCPU / 2 GB |

## Memory

- `process_rss_mb`: current resident memory.
- `process_max_rss_mb`: historical peak for this process.
- Investigate monotonic growth, not a single high reading.
- Auto/local mode intentionally load an ONNX model; a one-time higher baseline is expected.

## Facts

- `facts_active_count`: number of active fact rows.
- `facts_watermark_effective`: configured archive/refinement threshold.
- `watermark_warning` is true when active facts exceed the effective threshold.
- Threshold basis (v20.3.2, 2026-09-03): the shipped default of 800 is a conservative constant from v20.1, not a measured limit. On the reference production box, 1303 fact rows occupy a 4.9 MB `facts.db` with a 484 MB service RSS and all health probes green; the operator set `AIDUMEI_FACTS_WATERMARK=3000` (≈2.3× the current count) with two review triggers: `facts.db` above 15 MB or recall P95 above 300 ms. Raise the threshold only with a measurement like this; `refine_memory` without an LLM gear is lossy (v20.0 measured 20 memories → 1 summary line) and is not the default remedy.
- If the warning appears, run the consolidation workflow rather than silently increasing the threshold.

## WAL

- `wal_total_bytes`: total size of SQLite WAL sidecars.
- `wal_alert_dbs`: databases whose WAL is materially larger than the main database.
- Large WAL values increase crash recovery time and can invalidate timestamp-based reasoning.
- Run a checkpoint or use the scheduled database maintenance job before considering storage action.

## Disk

A production deployment needs capacity for:

- SQLite facts/history/state databases;
- embedded Qdrant vector storage;
- the local embedding model cache when `auto` or `local` is enabled;
- verified backups and rollback bundles;
- logs rotated by the deployment policy.

## Backup capacity

Use `scripts/backup_gate.sh create` and `verify`. Each backup stores SQLite snapshots plus checksums. At minimum, retain:

- one latest verified daily backup;
- one verified pre-upgrade backup;
- one known-good rollback point outside `/tmp`.

## Actions

| Signal | Action |
|---|---|
| `health_status != ok` | Inspect authenticated `/health` and `degraded_details`. |
| facts watermark warning | Run consolidation/refinement and inspect `report.py`. |
| WAL alert | Run database checkpoint maintenance and re-read health. |
| RSS steadily grows | Compare RSS over time and inspect memory-heavy optional dependencies. |
| Backup unverified | Run `backup_gate.sh verify` or create a new verified backup. |
