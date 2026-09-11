# `/health` Field Guide

The health endpoint is a diagnostic surface, not a guarantee that memory works. Use `scripts/e2e_smoke.py` for write→recall→trace→cleanup verification.

## Probe endpoints by cost tier (v20.4)

| Endpoint | Cost | Auth | Purpose |
|---|---|---|---|
| `/livez` | O(1): uptime + version only; no disk, DB, or singleton access | Public | Liveness for load balancers / orchestrators — if the process answers, it is alive. |
| `/readyz` | O(ms): four cheap local checks (`facts_db`, `text_fts_db`, `data_dir_writable`, `schema_version`); 503 + `failed` list when any fails | Public (names and booleans only, never paths) | Readiness — pull the instance from rotation when a fatal local precondition is broken (e.g. wrong `DATA_DIR`). |
| `/health` | Full deep probe; anonymous callers get a 30s-cached redacted public view | Public + token for full view | Backward-compatible contract used by e2e smoke, drills, MCP, and host plugins. |
| `/diagnostics` | Same full deep probe as authenticated `/health`, always fresh | Token/session required (401 otherwise) | Operator diagnostics without the anonymous fallback view. |

The mem0 singleton is deliberately **not** a `/readyz` check: it initializes lazily, so a cold-started instance would flap 503 until its first request. Its state is the `mem0_singleton` probe in `/diagnostics`.

## Why `runtime_paths.data_dir_writable` is visible to anonymous callers (v20.4.1b 裁决)

Anonymous `/health` responses redact all probe details (`probes._redacted`) except
`runtime_paths`. This is deliberate, not an oversight:

- Only **booleans** are exposed — never the path itself. `data_dir_writable: true`
  tells an attacker nothing they can act on.
- It is the only self-service signal for the most common first-run failure:
  "deployed, but data is being written somewhere I didn't intend" (wrong
  `DATA_DIR`, read-only bind mount). Without it, a new operator has no way to
  distinguish misconfiguration from a bug before they hold credentials.
- Everything sensitive (counts, versions of components, degradation detail)
  requires the token.

If your threat model forbids even this boolean, put the service behind your
reverse proxy's ACL — but the default loopback-only binding already covers
the common case.

## `/health` fields

| Field | Meaning | Healthy value | Failure direction |
|---|---|---|---|
| `health_status` | Overall endpoint health | `ok` | Inspect `degraded` and `warnings` |
| `version` | Runtime service version | expected version | Wrong deployment or stale process |
| `degraded` | Components currently unavailable | `[]` | Each item must have `degraded_details` |
| `degraded_details` | Reasons and sources for degraded items | list explaining every item | Missing reason means observability debt |
| `probes.facts_db` | Facts DB file present | `true` | Wrong `DATA_DIR` |
| `probes.text_fts_db` | Full-text DB present | `true` | Wrong `DATA_DIR` |
| `probes.mem0_singleton` | Memory runtime initialized | `true` | Configuration or backend failure |
| `probes.port_service` | Local network stack usable | `true` | Host networking restriction |
| `probes.runtime_paths` | Actual runtime directories and writability | intended writable paths | Delivery template or bind-mount mismatch |
| `probes.injection_guard_mode` | Effective injection defense mode | `enforce`, no config error | Illegal mode defaults to enforce |
| `probes.entity_keywords_ok` | Host-specific entity words configured | `true` when needed | Queries about host names can silently miss |
| `probes.vector_backend` | Vector backend name | expected backend | Wrong backend configuration |
| `probes.vector_backend_ok` | Vector backend health | `true` | Inspect `vector_backend_error` |
| `probes.local_embed` | Local fallback status object | `available: true` for auto/local | Missing optional dependency or model |
| `probes.default_bank_id` | Active default memory bank | expected bank | Misconfigured scope |
| `probes.memory_banks_ok` | Bank schema state | `true` | Migration or schema failure |

## Interpretation rules

- A `false` probe is valid when the corresponding optional feature is intentionally not deployed; it must still be attributed.
- A field that is always true is not a probe and must not be displayed as one.
- `health_status: ok` never proves semantic recall. Run the e2e smoke after installation and upgrades.

## Authenticated full-probe fields

The public allow-list intentionally excludes deep diagnostics. With a valid API token or session, `/health` also returns:

| Group | Fields | Meaning |
|---|---|---|
| Runtime | `modules`, `probes`, `service`, `warnings` | Complete diagnostic state and operator hints. |
| Scope | `probes.default_bank_id`, `probes.memory_banks_ok` | Active default memory bank and schema status. |
| Paths | `probes.runtime_paths` | Actual `BASE_DIR`, `DATA_DIR`, `LOG_DIR`, `facts_db`, writability, package-escape detection, and `path_consistency` — a WARNING when the mem0 config's `vector_store.config.path` / `history_db_path` do not resolve under the effective `AIDUMEM_DATA_DIR` (changing that variable relocates only SQLite; see docs/DEPLOY_DOCKHOLD.md). |
| Recall | `probes.vector_backend*`, `probes.local_embed`, `probes.rerank_*`, `probes.recall_verdict_threshold_effective` | Which recall legs are configured, reachable, or degraded. |
| Gears | `probes.engine_gear`, `probes.llm_gear`, `probes.engine_mode_policy` | Active gear, breaker state, thresholds, cooldown, and policy-disabled legs. |
| Capacity | `probes.facts_active_count`, `probes.facts_watermark_effective`, `probes.wal_total_bytes`, `probes.process_rss_mb`, `probes.process_max_rss_mb`, `probes.process_open_fds`, `probes.process_threads` | Fact size, WAL recovery pressure, and process resource pressure. |
| Reliability | `probes.feature_failures*`, `degraded_details`, `warnings` | Explicit ledger of soft-failed memory work and next actions. |
| Security | `probes.injection_guard_mode`, `probes.auth_gate_enabled`, `probes.auth_api_token_set`, `probes.auth_ui_password` | Effective security mode. Credentials are never returned. |
| Maintenance | `probes.core_memory_*`, `probes.core_replica_*`, `probes.schema_version*` | Stale core blocks, replica gaps, and on-disk schema version. |

## Never treat as normal

- A field named `*_ok` is false without a matching warning.
- `degraded_details` contains `probe_no_reason`.
- `auth_gate_enabled` is false while the service listens beyond loopback.
- `schema_version_ok` is false.
