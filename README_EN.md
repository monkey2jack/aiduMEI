<p align="center">
  <img src="assets/aidumei-banner.webp" alt="aiduMEI ☤ 爱嘟优忆思 — Memory + Engine + Insight" width="100%">
</p>

<!-- distribution-policy: github-source-only -->

# aiduMEI ☤ YouiSi — a long-term memory engine for AI agents

aiduMEI is a **single-machine self-hosted** long-term memory service (Python / FastAPI, MIT licence). It runs on your own machine and hooks into AI hosts such as Claude Code, Codex CLI and Hermes: before each turn it injects relevant memories into the model, after each turn it stores that turn, and when a session ends it distils one summary of "what this session was about". The host keeps short-term conversation state; aiduMEI keeps long-term memory. All data lives in a local data directory and can be backed up, deleted and restored.

> The current public release is **f0.7** (package `0.7.0`, 2026-10-29). [Download the Release](https://github.com/monkey2jack/aiduMEI/releases/tag/f0.7). Version-by-version history is in the [CHANGELOG](CHANGELOG.md).
>
> Current code: **f0.7.1** (an audit-fix patch on f0.7, internal candidate, no separate Release; the public Release remains f0.7; package `0.7.1`, 2026-10-30). Fixes are listed in the [CHANGELOG](CHANGELOG.md).

## What changed in f0.7

f0.7 swaps the foundation: the memory kernel moves from mem0 to the in-house **native single-SQLite kernel**, and mem0 is no longer a dependency.

- **native kernel**: a memory's text, full-text index, vector, edit history, tombstones and relation edges are written to one `memory.db`, one transaction per write. **Every tier uses it by default**; the one exception is a mem0 deployment upgraded from an older release and not yet migrated, which automatically stays on mem0 and logs a migration hint (see [Day-to-day operations](#day-to-day-operations)).
- **mem0 becomes optional**: `pip install -r requirements.txt` no longer installs `mem0ai` / `qdrant-client`; install `requirements-mem0.txt` (or `pip install ".[mem0]"`) only to run the legacy mem0 kernel, migrate from mem0, or roll back. A default native process never loads mem0.
- **One command to wire a host**: `scripts/connect_host.py` detects Claude Code / Codex / Hermes, wires the read, write and distil lines, and verifies them on the spot.
- **Memory checkup and nebula**: the console and `GET /graph/checkup` list duplicated, expired and conflicting memories; `GET /graph/nebula` draws the real relation graph on demand.
- **"Test connection" in settings**: one minimal real call each to LLM / Vision / Embedding / Reranker / decision model; new "use local vLLM" preset.
- **Cleaner deletes**: deleting a memory also removes its raw-text rows, the facts derived from it and the session summaries sourced from the deleted raw text; sentences with relative dates no longer leave date facts behind; still restorable from tombstones.
- **Smaller surface**: HTTP routes are now an allow-list and MCP tools went 41 → 6. No public benchmark ranks are claimed any more; quality is judged by a real-usage regression set.

## Core strengths

Every row points at code or tests in this repository.

| Capability | What it means | Where |
|---|---|---|
| **native single-SQLite kernel** | Text, FTS5 trigram full-text index, vectors, history, tombstones and relation edges share `memory.db` and one transaction per write. No separate vector-database service; `/add` does not call an LLM to rewrite each turn. Retrieval = FTS5 trigram (bm25 ranking) plus vector cosine, fused with RRF. Vectors use an OpenAI-compatible embedding endpoint; when none is configured, or in the local tier, retrieval is full-text only and text is still recallable | `ducky/memory_kernel/impl_native.py` |
| **Raw text kept as said** | The native kernel stores each message verbatim, without LLM rewriting; the session summary is a separate memory and never replaces the raw text | `ducky/hot/add.py` · `ducky/session_distill.py` |
| **Complete delete, restorable** | A single delete also removes vector and full-text entries, the memory's raw-text rows, the facts derived from it and the session summaries sourced from the deleted raw text (other independent memories with the same words are not touched). A tombstone snapshot is taken first and `POST /tombstone/restore` brings it back; snapshot text is wiped after 30 days by default (`AIDUMEI_TOMBSTONE_TTL_DAYS`, 0 = never). Results are reported honestly as `committed` / `partial` / `not_found` | `ducky/wal_engine.py` · `ducky/tombstone.py` |
| **One command to wire a host** | `python scripts/connect_host.py`: back up the host config → idempotently write the three hooks (commands point at the repo scripts, not copies, so upgrades never leave stale files) → run one synthetic conversation through the new hooks (store a nonce → recall it from a new session → delete the test memory). The first line is always `服务 ✅/❌ · 宿主接入 ✅/❌` (service / host wiring) | `scripts/connect_host.py` |
| **Alarm when the write line is missing** | With only the read line wired everything looks fine while every new sentence is lost — this project's most expensive incident. `/health` now carries `host_wiring` with the last host write; if none arrives within 30 minutes of start-up it shows up in warnings | `ducky/host_wiring.py` |
| **Memory checkup** | `GET /graph/checkup` answers three read-only questions: which text is stored several times, what has expired, and which topic currently holds several different values. Fixes still go through `/delete` and `/update`, and deletes still produce tombstones | `ducky/memory_checkup.py` |
| **Local tier makes no outbound calls** | With `AIDUMEI_ENGINE_MODE=local`, LLM, cloud embedding and rerank never send a request; tests replace the HTTP sender with one that fails on any call | `ducky/engine_mode.py` · `tests/test_v20_2_4_audit_remediation.py` |
| **Settings: test connection** | `POST /config/test` makes one minimal real call per model section and never echoes keys; LAN model hosts must be allowed explicitly via `AIDUMEI_MODEL_ENDPOINT_ALLOW`, cloud metadata addresses are always blocked | `ducky/routes_config.py` · `ducky/config_probe.py` |
| **Console** | `http://127.0.0.1:8767/ui`: browse/search memories, health and gear, memory checkup, nebula, model settings; zero-build static frontend | `frontend/` |

## Quick start

### Option 1: let your agent deploy it (recommended)

Send this sentence to your AI agent:

> You are now a deployment engineer. Follow the full `prompts/install.txt` (the 14-line canon) from <https://github.com/monkey2jack/aiduMEI> to deploy aiduMEI on this machine. Verify every step yourself; never pretend success.

The canon walks it through: environment check → dependencies → engine mode → keys (it asks you, never invents them) → start the service → real write/recall verification → `connect_host.py` → cron jobs and first backup → a report. The full agent-side guide is the [🤖 Agent Guide](AGENTS.md).

### Option 2: by hand

Python 3.10–3.12 (3.12 recommended).

```bash
git clone https://github.com/monkey2jack/aiduMEI.git && cd aiduMEI
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp mem0_config_local.json.example mem0_config_local.json   # model config: LLM and embedding URLs/keys (optional in local tier)
cp .env.example .env                                         # set AIDUMEM_API_TOKEN
python api_server.py                                         # → http://127.0.0.1:8767
```

Pick one engine mode (`AIDUMEI_ENGINE_MODE=auto` is the default):

| Mode | Needs | Effect |
|---|---|---|
| `cloud` | Cloud LLM + embedding keys | Lightest; during a cloud outage recall honestly reports `degraded` |
| `auto` (default) | Keys + `pip install .[local-embed]` + `python scripts/fetch_local_embed_model.py` | Cloud first; under the native kernel an embedding outage falls back to full-text search (the local vector fallback exists only under mem0); switches back on recovery |
| `local` | No keys | Zero tokens, zero outbound calls; native kernel by default, full-text retrieval only; session summaries need an LLM |

Changing mode requires a restart.

### Wire the host and verify

```bash
python scripts/connect_host.py            # detect + wire + verify; --dry-run shows changes only, --check verifies only
python scripts/e2e_smoke.py --json        # writes a nonce → recalls it from a new request → cleans up; PASS with 0 warnings
curl -s -H "Authorization: Bearer $AIDUMEM_API_TOKEN" http://127.0.0.1:8767/health | jq '.health_status, .degraded, .probes.runtime_paths'
```

Three probes on `/health` first: `health_status` is `ok`, `degraded` explains every unavailable component, `probes.runtime_paths.data_dir_writable` is true. `/health` alone is not enough — it can be all green while the write line is not wired. After a few real turns, re-check with `python3 scripts/check_ingest_wiring.py --require-judgment` (credentials via environment or `.env`, never `--token`: command lines are visible to other local users through `ps`).

Hosts that `connect_host.py` does not know (e.g. Cursor) are wired by hand per [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md), then checked with `python scripts/agent_integration_check.py`.

### Container deployment (Docker Compose)

Three things must be in place before `docker compose up -d`:

1. **Credentials**: put `AIDUMEM_API_TOKEN=<long random string>` in the `.env` next to `docker-compose.yml`. Inside the container the service binds `0.0.0.0`, and a non-loopback bind without credentials refuses to start; compose references `${AIDUMEM_API_TOKEN:?…}`, so a missing token fails immediately. Compose only uses `.env` for interpolation; other keys do not reach the container — to change the kernel or other settings, add them to the compose `environment`.
2. **Directory ownership**: `mkdir -p data logs && sudo chown -R 10001:10001 data logs` (the container runs as uid 10001; the symptom of skipping this is `unable to open database file` on the first write).
3. **Model config inside the data directory**: `cp mem0_config_local.json.example data/mem0_config_local.json` and fill in keys (the container reads `/app/data/mem0_config_local.json`). (mem0 kernel only) Keep the vector store path `vector_store.config.path` under `./data`, or a container rebuild loses it.

Afterwards `docker compose ps` should show `healthy` (it probes `/livez` inside the container); then run `docker compose exec aidumem python scripts/e2e_smoke.py --json`. Hosted platforms: [docs/DEPLOY_DOCKHOLD.md](docs/DEPLOY_DOCKHOLD.md).

## Day-to-day operations

| Task | Command |
|---|---|
| Health | `/health` (three probes above); `python scripts/report.py --json` (exit 0 healthy / 2 warning / 3 degraded) |
| Install cron jobs | `bash scripts/update_crontab.sh install`, then compare each entry with `bash scripts/update_crontab.sh --installed` |
| Backup | `bash scripts/backup_gate.sh create <name>` → `bash scripts/backup_gate.sh verify <backup_dir>` (whole data directory incl. `memory.db`, SQLite online snapshots + checksums) |
| Restore (isolated drill) | `bash scripts/restore_gate.sh --dry-run <backup_dir>`; a real restore only writes into a **new, non-existent** directory, see [docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md) |
| Undo a single delete | `GET /tombstones` → `POST /tombstone/restore` |
| native DB check (read-only) | `python scripts/native_preflight.py --data-dir <data_dir>` |

**Backups are plaintext**: every memory and any credentials inside the data directory are in the backup; treat it like live data. There is no built-in encryption; use full-disk encryption on the machine holding backups.

**Upgrade**:

1. Back up and verify; run `bash scripts/pre-upgrade-check.sh`.
2. `git pull` → `pip install -r requirements.txt` → restart the service.
3. Re-run `python scripts/connect_host.py` (idempotent; hooks point at the repo scripts and are verified again). Older Hermes installs with hand-copied hooks: run `python3 scripts/check_hook_deployment.py` — it compares the paths declared in `~/.hermes/config.yaml`; exit code 0 means deployed.
4. `python scripts/e2e_smoke.py --json` → `bash scripts/post-upgrade-check.sh`.

**Moving from the mem0 kernel to native** (once, when upgrading an old deployment to f0.7; until then the service stays on mem0 and logs a hint): `pip install -r requirements-mem0.txt`, then with the service stopped run `python scripts/migrate_to_native.py --data-dir <old_data_dir> --out <new memory.db>` (sources read-only, re-runnable, with a reconciliation report; `--embed-missing` fills missing vectors and makes outbound calls), put the generated `memory.db` in the data directory and restart; native is then used (you may also set `AIDUMEI_MEMORY_KERNEL=native` explicitly).

**Rollback**: a code rollback restores code, hooks and only its own configuration changes — **never overlay old backup data onto writes received after the snapshot** ([docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md)). Before switching the kernel from native back to mem0, `pip install -r requirements-mem0.txt`, stop the service and run `python scripts/native_to_mem0_resync.py --data-dir <data_dir> --since <time you switched to native>` (preview by default, `--apply` to write) to replay writes, edits and deletes made under native into the old stores; then set `AIDUMEI_MEMORY_KERNEL=mem0`.

## Key configuration

`.env` sits in the repository root and is read at start-up (variables already in the environment win). The full registry is `ducky/env_registry.py`; `.env.example` has the commented list.

| Variable | Purpose | Default |
|---|---|---|
| `AIDUMEM_API_TOKEN` | API auth token; mandatory for non-loopback binds | empty = loopback only |
| `AIDUMEM_UI_PASSWORD` | Console passphrase (stored with PBKDF2) | empty |
| `AIDUMEM_DATA_DIR` | Data directory | `data/` under the repo |
| `AIDUMEM_API_PORT` | REST port | 8767 |
| `AIDUMEI_ENGINE_MODE` | Engine mode `cloud` / `auto` / `local` | `auto` |
| `AIDUMEI_MEMORY_KERNEL` | Memory kernel `native` / `mem0` / `shadow`; unknown values fail at start-up, no silent fallback | `native` (automatically `mem0` for an unmigrated old mem0 deployment) |
| `AIDUMEI_TOMBSTONE_TTL_DAYS` | Tombstone snapshot retention in days, text wiped on expiry; 0 = never | 30 |
| `AIDUMEI_MODEL_ENDPOINT_ALLOW` | Allow LAN/tailnet model hosts (comma-separated hostnames or IPs) | empty |
| `AIDUMEI_RERANKER_API_KEY` | Rerank key, overrides the config file | empty |
| `AIDUMEI_CALLER_BINDINGS` | Credential fingerprint → principals it may act for | empty |
| `AIDUMEI_CALLER_BINDING_MODE` | `strict` / `permissive` / `off` | `off` |
| `AIDUMEI_MCP_DANGEROUS_TOOLS` | Set to 1 to register MCP `mem_update` / `mem_delete` | off |
| `AIDUMEI_SKIP_PLATFORMS` | Sessions from these host platforms are neither stored nor distilled (Hermes hooks and plugin); empty string disables | `cron,subagent` |
| `AIDUMEI_INJECT_DATE` | Whether injected memories carry a time `day` / `minute` / `off` (hook side) | `day` |

New variables always use the `AIDUMEI_` prefix; `AIDUMEM_` is the legacy prefix and existing keys are not migrated. Model URLs and keys live in `mem0_config_local.json` (legacy file name; the native kernel reads its embedding config from there too) or in the console settings page with "test connection". Once a reranker is configured, eligible searches use it automatically; see [docs/DECISION.md](docs/DECISION.md) and `.env.example`.

## Integrations

| Host / channel | How |
|---|---|
| Claude Code | `connect_host.py` writes `~/.claude/settings.json`: `UserPromptSubmit` read / `Stop` write / `SessionEnd` distil (`integrations/claude-code/`) |
| Codex CLI (≥ 0.163) | `connect_host.py` writes `~/.codex/hooks.json`, same three lines (`integrations/codex/`); non-managed hooks must be trusted once in Codex |
| Hermes Agent | `connect_host.py` symlinks the plugin `integrations/hermes-plugin/aidumem` and sets `memory.provider aidumem` |
| Cursor and other hosts | Wire by hand per [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md) |
| HTTP API | REST `:8767`: `/add` `/search` `/update` `/delete` `/session/distill` `/tombstones` `/graph/*` `/api/core-memory` and more; write endpoints take `caller_user_id` |
| MCP | See next section |

## MCP Server (6 tools · default port 8766)

`mcp_server.py` uses the official SDK `mcp==1.30.0` (already in `requirements.txt`), stdio / HTTP transports, default port 8766. Tools: `mem_add`, `mem_search`, `mem_health`, `core_memory_list`, plus `mem_update` and `mem_delete`, which are **not registered** by default (set `AIDUMEI_MCP_DANGEROUS_TOOLS=1`).

Auth: a non-loopback bind must configure `AIDUMEM_API_TOKEN` or the server refuses to start. Only set `AIDUMEM_ALLOW_INSECURE_PUBLIC=1` if you truly need credential-less public exposure, and then also `AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH=<listen address>` (must equal the actual listen address verbatim); the REST service follows the same rule. Repeated failures of the same tool with the same arguments on one connection are stopped by a loop guard (`AIDUMEI_MCP_LOOP_GUARD`, default 5 failures / 60 s / 30 s cool-down, per process).

## Security Model

- **Credentials**: Bearer token `AIDUMEM_API_TOKEN`; console passphrase with PBKDF2; loopback-only by default. Check the Three probes first (above); without credentials `/health` probes are redacted with a `_redacted` note.
- **Single owner**: the service accepts one API token and `caller_user_id` is self-declared by the client. **Isolation between mutually untrusted users is not supported**; one service per owner (or agents that trust each other). To restrict principals per credential, configure `AIDUMEI_CALLER_BINDINGS` + `AIDUMEI_CALLER_BINDING_MODE=strict`.
- **Outbound**: model URLs accept public https or localhost by default; private and cloud-metadata addresses are blocked (LAN model hosts can be allowed with `AIDUMEI_MODEL_ENDPOINT_ALLOW`), and model calls do not follow redirects. What each cloud channel sends is listed in [docs/OUTBOUND_CHANNELS.md](docs/OUTBOUND_CHANNELS.md).
- **Injection defence**: recalled content is neutralised and fenced before injection; destructive MCP tools are off by default; `delete_all` needs explicit `confirm=true`.
- External audits are logged line by line: [docs/SECURITY-AUDIT-LEDGER.md](docs/SECURITY-AUDIT-LEDGER.md), `SECURITY.md`.

## Known Limitations & Not Covered

| # | Limitation | Notes |
|---|------|------|
| 1 | The legacy mem0 kernel remains as an option | The mem0 / shadow kernels and the migrate / rollback scripts are still in the code and need `requirements-mem0.txt`; the default native kernel neither installs nor loads mem0. The model config file keeps its legacy name `mem0_config_local.json`; the FTS5 text field written back on tombstone restore borrows mem0's lemmatizer when mem0 is installed and uses the raw text otherwise (`ducky/tombstone.py`) |
| 2 | "Single SQLite" means the memory kernel, not one file for the whole service | `memory.db` holds memories; deterministic facts and core memory stay in `facts.db` (`ducky/memory_kernel/impl_native.py` names facts.db as the single fact source), plus auxiliary stores such as the mutation journal; back up the whole data directory |
| 3 | native has no local vector fallback | When the embedding endpoint fails, or in the local tier, it falls back to FTS5 trigram full-text search, so paraphrased queries recall less; in `ducky/engine.py` native skips the auto tier's local fallback leg. native supports OpenAI-compatible embeddings only |
| 4 | Tenant isolation narrows visibility per tenant | It is not a strong isolation layer between mutually distrusting customers; the domain contract is `ducky/bank_contract.py` |
| 5 | The local embedding model must be fetched at deploy time | Zero network at runtime: `ducky/local_embed.py` forces `HF_HUB_OFFLINE=1`; fetched files are verified against `scripts/local_embed_model_sha256.json` |
| 6 | Writes are not atomic across stores | A write journal and integrity chain exist; ambiguous writes need verified repair, exactly-once is not promised; the hash chain is not a signature |
| 7 | Platforms | The full suite is maintained on Linux/macOS; on Windows the service starts, but cross-process write protection, the backup gate and ops scripts are POSIX-only |

## Testing & quality

**How quality is measured**: no leaderboard chasing and no benchmark ranks are claimed. Quality is judged by the **real-usage regression set v3** (171 cases: fact recall, update, delete, temporal and abstain, plus 20 Claude Code host replays): zero tokens, local, under a minute. `scripts/push_gate.sh` runs it against both the mem0 and native kernels and blocks a merge if any category drops below its baseline. The real set comes from the maintainer's own (desensitised) usage and is **not shipped in the public repo**; the public repo generates a fictional set with the same structure via `benchmarks/build_public_regression.py`, and the two sets' scores are not comparable. Run: `python -m benchmarks.regression --baseline benchmarks/regression_v3_baseline_native.json` (with `AIDUMEI_MEMORY_KERNEL=native`).

**Test levels, stated honestly**
> How to read the table: "passed · skipped" per row sums to the `pytest --collect-only` count **for that shape, on that date**; different rows may have different denominators (the tree grows), so trust the date in each row. Skips are explained per "axis" (see [docs/TESTING.md](docs/TESTING.md)) and are not failures.

| Dimension | Current |
|------|------|
| Total cases | **3907** (measured via `pytest --collect-only`, 2026-10-30, f0.7.1 tree) = **3279 behavior + 482 script/hook + 146 guard**; the split is recomputable with `scripts/count_test_kinds.py` |
| Clean dev machine | 3895 passed · **12 skipped** — f0.7 acceptance target derived from the collection and the missing axes (Python 3.12, only the Hermes host source absent) |
| Basic install path | 1821 passed · **25 skipped** — requirements files only (**measured 2026-09-09 on the production box**, clean venv, older tree). From f0.7 the full suite also tests the legacy mem0 kernel and needs `requirements-mem0.txt`; the default native path with only `requirements.txt` is covered by a from-scratch install acceptance and the CI bare-install smoke (2026-10-29 lab box: install, health, write, recall, delete, restore passed) |
| Sandbox on the production box | 1967 passed · **26 skipped** — **measured 2026-09-11** (older tree `de09794`, host source present, no `.env`, no ruff/mcp/fastembed) |
| All axes present | 1844 passed · **1 skipped** — **measured 2026-09-09 on the production host** (older tree; tools, extras, host, model cache and the public LoCoMo dataset present) |
| Levels | Mostly module-level unit tests plus source-level guard assertions, with `TestClient`-driven interface tests as support |
| Not covered | Real providers, host CLIs and production performance need separate evidence and are not proven by case counts; statement coverage has not been re-measured |

```bash
pip install -r requirements.txt -r requirements-mem0.txt -r requirements-dev.txt
pytest tests/                                          # full regression
python -m compileall ducky api_server.py mcp_server.py # compile check
```

> **Why report both 3895 and 1821**: the first is this tree's expected pass count with only the 12 Hermes cases skipped, not a measured result; the second is the 2026-09-09 clean-venv measurement of the basic install path on an older tree. A number only means something with its environment, date and tree attached.

> **You can verify those 12 skips yourself**: all fourteen skip axes are registered in [docs/TESTING.md](docs/TESTING.md); `HERMES_SRC` is tri-state and reproducible in both directions:
>
> ```bash
> pip install "mcp==1.30.0" ruff nltk regex numpy fastembed && python scripts/fetch_local_embed_model.py
> pytest tests/ -q -rs | tail -1                                 # no host: 3895 passed, 12 skipped
> HERMES_SRC=/path/to/hermes-agent pytest tests/ -q | tail -1    # target once the host and all other optional axes are present — with host: 3907 passed
> HERMES_SRC=none pytest tests/ -q -rs | tail -1                 # forced off: 3895 passed, 12 skipped
> ```
>
> The "with host" line needs all fourteen axes at once. On the production box in an isolated sandbox (host source present, no `.env`), the bare command on an older tree actually prints 1967 passed, 26 skipped (2026-09-11, `de09794`).
>
> | Skip axis | Gated cases | Location |
> |---|---:|---|
> | Host Hermes source | 12 | `tests/test_hermes_plugin.py` |
> | git worktree | 1 | `tests/test_v20_brand_policy.py` |
> | `scripts/backup_gate.sh` + POSIX shell | 8 | `tests/test_v19_4_1_backup_gate.py` |
> | WAL POSIX real locks and directory fsync | 5 | POSIX tests in `tests/test_f03pp_wal_integrity.py` |
> | `qdrant_client` installed | 1 | `tests/test_v20_vector_bank_contract.py` |
> | LoCoMo dataset present | 1 | `tests/test_v20_locomo_official.py` |
> | `regex` installed | 1 | `tests/test_v20_locomo_official.py` |
> | `numpy` installed | 1 | `tests/test_v20_locomo_official.py` |
> | `nltk` installed | 13 | `tests/test_v20_locomo_official.py` and `tests/test_v20_benchmarks.py` |
> | `git` executable present | 6 | `tests/test_v20_gitignore_guard.py` |
> | `mem0ai` installed | 20 | `tests/test_v20_mem0_patch_layer.py` |
> | `fastembed` installed | 1 | `tests/test_v20_2_autoshift.py` |
> | `ruff` installed | 3 | static rules F821/F811/F841 |
> | `mcp` extra installed | 9 | MCP import surface, auth, SSE transport and search session passthrough |

## Resource usage: f0.7 vs f0.6

Same 4-core / 8 GB cloud host (2026-10-29); each version installed from scratch and run twice, ranges shown; the auth gate was on for both. Embeddings/LLM come from a local stub (fixed 1024-dim vectors), so only aiduMEI and its vector store are measured, not model inference. f0.6 is measured as it ran in production: mem0 kernel + a Qdrant container (v1.20.0); f0.7 uses the default native kernel.

**Out of the box (installed, started, idle)**

| Item | f0.6 (mem0 + Qdrant) | f0.7 (native) | Change |
|---|---|---|---|
| Direct deps / installed packages | 12 / 55 | 10 / 36 | 19 fewer packages |
| Python environment | 174 MB | 98 MB | −44% |
| Qdrant image required | 276 MB | none | −100% |
| Total install footprint | about 450 MB | about 98 MB | −78% |
| Resident processes | 2 (service + Qdrant) | 1 | |
| Idle memory | 179 MB | 63 MB | −65% |
| Start to ready | 1.8 s | 0.6 s | 3× faster |

**In use (2,000 writes, then 300 searches)**

| Item | f0.6 (mem0 + Qdrant) | f0.7 (native) | Change |
|---|---|---|---|
| Memory (service + vector store) | 207 MB | 112–113 MB | −46% |
| Service process peak | 171–176 MB | 127–128 MB | −27% |
| Disk (all databases) | 46 MB (SQLite 25 + Qdrant 21) | 40 MB (single DB) | −13% |
| Write P50 / P95 | 76 / 102–109 ms | 63–64 / 97–99 ms | slightly faster |
| Search P50 / P95 | 17–19 / 24–26 ms | 30–34 / 38–42 ms | about 13–15 ms slower |

Notes:
- Slower search is a known trade-off: native does exact vector comparison inside the single database instead of Qdrant's approximate index; at a personal scale of a few thousand memories it stays under 45 ms.
- Real production memory is higher than this table: a real embedding client, a larger memory store and background jobs all take memory. The table is a like-for-like comparison between versions.
- The `local` tier (with a local embedding model) has not been re-measured; it adds the model runtime and weights on top.
- Reproduce: `python scripts/slim_bench.py --code <code dir> --venv <venv> --label f0.7`; for the old version add `--qdrant --kernel mem0` (needs Docker).

## Docs index

| Topic | Documents |
|---|---|
| Agent deploy and ops entry | [AGENTS.md](AGENTS.md) · [prompts/install.txt](prompts/install.txt) |
| Host integration | [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md) · [integrations/INTEGRATION_GUIDE.md](integrations/INTEGRATION_GUIDE.md) |
| Health and troubleshooting | [docs/HEALTH.md](docs/HEALTH.md) · [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| Ops, backup, upgrade, rollback | [docs/OPERATIONS.md](docs/OPERATIONS.md) · [docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md) |
| Outbound and security | [docs/OUTBOUND_CHANNELS.md](docs/OUTBOUND_CHANNELS.md) · [docs/SECURITY-AUDIT-LEDGER.md](docs/SECURITY-AUDIT-LEDGER.md) |
| Decision model (optional) | [docs/DECISION.md](docs/DECISION.md) |
| Test methodology | [docs/TESTING.md](docs/TESTING.md) · [docs/BENCHMARKING-POSTURE.md](docs/BENCHMARKING-POSTURE.md) |
| Versioning and history | [docs/VERSIONING.md](docs/VERSIONING.md) · [CHANGELOG.md](CHANGELOG.md) |

## Repository layout

```text
aiduMEI/
├── api_server.py            # main entry: REST API + /ui console
├── mcp_server.py            # MCP server (:8766)
├── ducky/                   # application code
│   └── memory_kernel/       #   swappable kernel: native (single DB) / mem0 (legacy) / shadow
├── frontend/                # console (zero-build static)
├── integrations/            # host hooks and plugin (claude-code / codex / hermes-plugin / cursor-hook)
├── scripts/                 # connect_host / e2e_smoke / backup_gate / migrate_to_native and more
├── benchmarks/              # real-usage regression runner and baselines
├── prompts/install.txt      # one-prompt deployment canon
├── docs/                    # ops, health, backup, integration, testing docs
└── tests/                   # pytest regression
```

## License

MIT — see [LICENSE](LICENSE).

<p align="center">
  <sub>aiduMEI ☤ YouiSi (formerly aiduMEM / duMem, kept in historical versions and docs) ｜ Powered by monkey²</sub>
</p>
