# AGENTS.md — Deploy, Verify, Operate aiduMEI

aiduMEI is a self-hosted long-term memory and wisdom engine for AI agents. It stores extracted facts, raw records, core memory, and recall traces behind a local HTTP API. It works with your host's native memory as the durable long-term layer; the host remains responsible for short-term conversation state.

## Choose your engine mode

| Need | Mode | Requires | Effect |
|---|---|---|---|
| Lowest footprint | `AIDUMEI_ENGINE_MODE=cloud` | Cloud LLM + embedding keys | No local model; during an outage recall is honestly `degraded` |
| Default resilience | `AIDUMEI_ENGINE_MODE=auto` | Cloud keys + `pip install .[local-embed]` + model fetch | Cloud-first, automatic local fallback and recovery |
| Fully local | `AIDUMEI_ENGINE_MODE=local` | Local embed dependencies and model; no API keys | Zero cloud tokens and zero outbound model calls |

## One-Line Prompt (canonical)

Send the canonical deployment text (all of [prompts/install.txt](prompts/install.txt), 13 lines) to your Agent — do not paraphrase it. Every step is judged by script exit codes and JSON evidence only.

Changing mode requires a service restart. If you previously ran `cloud`, run the local-vector backfill before expecting the local gear to recall older memories.

## Install and start

```bash
git clone https://github.com/monkey2jack/aiduMEI.git
cd aiduMEI
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp mem0_config_local.json.example mem0_config_local.json
cp .env.example .env
# Edit mem0_config_local.json: set llm.config and embedder.config.
# Edit .env: set AIDUMEM_ENTITY_KEYWORDS and, if exposing beyond loopback, AIDUMEM_API_TOKEN.
python api_server.py
```

For `auto` or `local`, also run:

```bash
pip install .[local-embed]
python scripts/fetch_local_embed_model.py
```

## Prove memory is actually working

Run the end-to-end smoke after the service starts:

```bash
python scripts/e2e_smoke.py --json
```

Expected output: JSON ending with `"status": "PASS"`, zero failures, and zero warnings; process exit code 0. `/health: ok` alone is not sufficient — this script writes a nonce, recalls it from a new request, checks trace visibility, and cleans up its temporary tenant.

## Three probes to check first

```bash
curl -s -H "Authorization: Bearer $AIDUMEM_API_TOKEN" http://127.0.0.1:8767/health | jq '.health_status, .degraded, .probes.runtime_paths'
# Without the token (auth gate on) probes are redacted: you get probes._redacted and only runtime_paths.data_dir_writable.
```

1. `health_status` must be `ok`.
2. `degraded` must explain every unavailable component.
3. `probes.runtime_paths.data_dir` must be the directory you intended to persist, and `data_dir_writable` must be true.

## Operate

- Health and failures: `docs/HEALTH.md`
- Daily and periodic jobs: `docs/OPERATIONS.md`
- Failure scenarios and runbooks: `TROUBLESHOOTING.md`
- Backup, restore, upgrade, rollback: `docs/BACKUP_RESTORE.md`
- Host integration and memory ownership: `docs/AGENT_INTEGRATION.md`
- Script index: `scripts/README.md`

### Self-report

```bash
python scripts/report.py --json
```

Returns machine-readable JSON: version, engine mode, gear state, degraded components, warnings, memory capacity, WAL, backup status, and next actions. Exit code 0 = healthy; 2 = warning; 3 = degraded.

### Drill autoshift

```bash
bash scripts/drill_autoshift.sh --check
```

Verifies that all scripts and health fields needed for a full autoshift drill are present. Use `--run` against a live service to verify gear state, pending replay, and warming-up fields.

### Restore gate

```bash
bash scripts/restore_gate.sh --dry-run <backup_dir>
```

Verifies backup checksums and SQLite integrity, then (in apply mode) restores and runs e2e smoke. Apply mode requires `RESTORE_GATE_ALLOW_APPLY=1`.

## End-to-end data flow

```mermaid
flowchart LR
    A[Agent /add] --> B[Coalesce optional]
    B --> C[LLM distillation when available]
    A --> D[Pattern extraction]
    A --> E[Raw and verbatim storage]
    C --> F[Cloud vector index]
    A --> G[Local vector index in auto/local]
    H[/search/] --> I[Workspace]
    I --> J[Hybrid recall]
    F --> J
    G --> J
    J --> K[Scoring and optional rerank]
    K --> L[Trace + verdict + engine mode]
    L --> M[Agent context]
```
