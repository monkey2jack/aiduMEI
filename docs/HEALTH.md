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
