# `/health` Field Guide

The health endpoint is a diagnostic surface, not a guarantee that memory works. Use `scripts/e2e_smoke.py` for write→recall→trace→cleanup verification.

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
| Paths | `probes.runtime_paths` | Actual `BASE_DIR`, `DATA_DIR`, `LOG_DIR`, `facts_db`, writability, and package-escape detection. |
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
