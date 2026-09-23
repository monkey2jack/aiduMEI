<p align="center">
  <img src="assets/aidumei-v20-banner.svg" alt="aiduMEI" width="100%">
</p>

<!-- distribution-policy: github-source-only -->

# aiduMEI ⚕ YouiSi — the Universal Wisdom Engine for AI Agents

> Let your AI Agent **actually remember you**: hybrid retrieval + governance + a visual console + dual-engine autoshift. A **single-machine self-hosted** engine, MIT.
> Your host (Hermes / Claude Code / Cursor / any MCP client) owns short-term conversation; aiduMEI owns long-term memory.

> The current public release is **f0.1** (formal) — **YouiSi: One-Line Prompt deployment, dual-engine autoshift, Pantheon multi-bot memory personas, first of its kind.** v21.2 adds the Memmy fusion set: echo suppression, MMR diversity, an error-signature channel, and trajectory-level reward credit assignment (weight defaults to 0 — installed but inert until our own data says otherwise). v21.1 lays the Pantheon foundation: multiple bots/profiles each in their own hall with independent memory personas and cross-hall isolation by default; includes v21.0 cognitive governance (epistemic origin tagging, knowledge provenance, one-click dossier export). See [CHANGELOG](CHANGELOG.md).

---

## Three things nobody else ships

| Edge | In one line |
|---|---|
| 🚗 **Dual-engine autoshift** | On cloud outage it downshifts to the local spare engine mid-query, upshifts on recovery, replays the debt — and the gear state is always honestly visible |
| 📊 **Visual console** | A zero-build web console at `/ui`: memories are visible, tunable, traceable — a direct answer to "memory is a black box" |
| 🧠 **Cognitive governance (v21)** | Every memory **has an origin** (user-stated / AI-inferred / externally-referenced / unknown, tagged at write time at zero LLM cost), **has provenance** (which agent, which session, which turn), and **is exportable** (one-click Markdown dossier); the AI may only *propose* candidate beliefs — it cannot *create facts* directly |

## One-prompt deploy: let your Agent do the work, you watch

Send this to your AI Agent:

> You are the deployment engineer. Deploy aiduMEI on this machine by following <https://github.com/monkey2jack/aiduMEI> `prompts/install.txt` verbatim (the 14-line canon). Verify each step yourself; never fake success.

The canon walks it through: environment check → install → gear selection → keys (it asks you; never invents) → service up → **real write/recall verification** (not just `/health`) → host integration → cron & backups → final report.

> ⚠️ **Wiring the host means wiring two hooks, not one.** The *read* hook injects memories *before* each turn — miss it and you notice immediately. The *write* hook persists the turn *afterwards* — **miss it and you will not notice for weeks**: retrieval still returns results and `/health` stays green, because the *old* memories really are healthy, while everything new you say is thrown away.
>
> We paid for this lesson on our own production deployment (2026-09-17: read hook live for a month, write hook never wired, every probe green). Hence `/health` now carries an `ingest_liveness_ok` probe for "reading but not writing", and after a few real turns you should run:
>
> ```bash
> python3 scripts/check_ingest_wiring.py --token "$AIDUMEM_API_TOKEN"   # exit code 0 means wired
> ```
>
> **All three wires ship as ready-made scripts** — copy and register them, don't write your own:
>
> | | Script | Hook | What it does automatically |
> |---|---|---|---|
> | Read | `integrations/aidumem-inject.sh` | Hermes `pre_llm_call` | Feeds relevant memories to the model **before** each turn |
> | Write | `integrations/aidumem-ingest.sh` | Hermes `post_llm_call` | Stores the turn **after** it finishes |
> | Write | `integrations/cursor-hook/claude-code-stop-hook.py` | Claude Code `Stop` | Same |
> | Distill | `integrations/aidumem-distill.sh` | Hermes `on_session_end` | On session close, distills "what this stretch was about" into its own memory |
>
> **"Automatic" means that once these three are wired you never touch memory again** —
> no manual saves, no reminding the model to remember, no periodic cleanup. The three
> hooks fire themselves at three moments: before you speak, after you speak, after you
> are done. Your only job is to attach them correctly and then run the check below once.
>
> The distill wire addresses a different kind of forgetting: per-turn writes store
> *facts*, and facts cannot hold "what this stretch was about" — the offhand remark,
> the problem solved together, the moment a decision was made. It uses its own slow
> decay lane (`distill`), and its emotional weight counts hits against the emotion
> keyword list this repo already ships, not an invented score.
>
> All three carry `--selftest` (the write one really writes a memory and reads it back). But a passing selftest only proves the script runs — **it does not prove the host is calling it**. In the incident above the scripts were fine the whole time; nobody had hooked the write one. That is why `check_ingest_wiring.py` is the only acceptance criterion. Full wiring and yaml in [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md).

**No Agent? Five manual lines:**

```bash
git clone https://github.com/monkey2jack/aiduMEI.git && cd aiduMEI
python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp mem0_config_local.json.example mem0_config_local.json && cp .env.example .env   # fill in your keys
python api_server.py                                                              # → http://127.0.0.1:8767
python scripts/e2e_smoke.py --json                                                # real verification; PASS or it didn't happen
```

**Three engine gears, pick by your machine** (`AIDUMEI_ENGINE_MODE`):

| Gear | Requires | Effect |
|---|---|---|
| `cloud` | Cloud LLM + embedding keys | Lightest; recall honestly `degraded` during an outage |
| `auto` (recommended) | Keys + `pip install .[local-embed]` | Cloud-first, automatic local fallback and recovery |
| `local` | Local model, zero keys | Zero tokens, zero outbound calls |

Container deployment: [docs/DEPLOY_DOCKHOLD.md](docs/DEPLOY_DOCKHOLD.md). The full agent-side runbook (acceptance probes, backups, maintenance): [AGENTS.md](AGENTS.md).

## 📦 Load & consumption — both sizes, measured and on the table

> How heavy is it to deploy? **Depends on the gear you pick.** (2-core 3.5GB cloud box, measured 2026-08-27)

| Dimension | ☁️ Cloud gear (`cloud`) | ⚙️ Auto gear (`auto`, default) | 🔋 Local gear (`local`) |
|---|---|---|---|
| **Resident memory** | **~280 MB** | **~430 MB** | ~430 MB |
| **Dependency disk** | ~275 MB | ~353 MB + 91 MB model | same as auto |
| **During an outage** | no spare; honestly reports `degraded` | **auto-downshift keeps running** | no external dependency at all |
| **Token burn** | normal | normal (zero during outages) | **always zero** |
| **Keys needed** | yes | yes (without them it just runs local) | **none** |

**Shared**: 2 CPU cores suffice, idle < 1%; `/search` 0.14~0.23s per call; cold start 5.2s; a thousand memories cost ~13 MB of vectors + a few hundred KB of SQLite; zero frontend dependencies; Python 3.10–3.12.

**Where the 150 MB goes, and whether it can be shaved** (measured, not estimated): the onnxruntime library alone (imported, model not loaded) **75 MB**; the bge-small-zh-v1.5 session & weights **~122 MB**; the measured two-gear delta **150 MB** (components measured separately share pages with the baseline — both figures honestly shown, no forced arithmetic). We tried to shrink it: `threads=1`, ONNX arena on-demand allocation, `malloc_trim`, `MALLOC_ARENA_MAX=2` — **all four knobs measurably did nothing**; the model is already the smallest Chinese-capable option in the fastembed catalog (the next-smallest multilingual option is 2.4× its size). So instead of pretending to optimize, we gave you the switch: **don't want the spare? Pick the cloud gear and the 150 MB costs you nothing.**

> **Why the spare is resident**: dual indexing computes a local vector for *every* write — load the model only when the outage hits, and nothing written so far is recallable. The spare is stocked in advance, not found on the spot.
> The rest of the lightness is deliberate: embedded on-disk vector store (no separate process/port), no GPU, a relevance gate that blocks chit-chat from ever triggering retrieval, SQLite+FTS5 underneath. In one line: **cloud gear runs on 1 core / 1 GB; auto and local gears want 2 cores / 2 GB.**

> **Pick the gear by your machine's memory (deployment Agents, follow this)**: plenty of free RAM (≥2G headroom) and you want "recall the full history even when the cloud is down" → `auto` (the local spare is a **resident hot-spare**; that ~174MB buys the outage resilience); tight on memory or no need for offline fallback → `cloud` (~280MB, honest `degraded` on outage, no local downshift). **Don't let a memory-constrained box default to `auto` and then complain it's heavy** — the gear is yours to pick per machine; run `free -m` first.
>
> **Planned (v21.2) cold-spare gear**: the local model is **not resident** (saves that ~174MB in normal operation), loaded only on a cloud outage to batch-recompute local vectors for existing memories; measured on a mid-size store (~1500 entries) the recompute window is on the order of ten-odd seconds, `degraded` during it, full recovery once done. It reconciles "save memory normally" with "full resilience after an outage," at the cost of a recompute window that grows linearly with store size (large stores need incremental recompute) — the direct answer to "the auto gear should load on demand." The hot-spare (today's `auto`) vs cold-spare (planned) trade-off will fold into the gear-selection guide.

## The console: memory is not a black box

Open `http://127.0.0.1:8767/ui` after starting: browse/search/tune memories, health probes and gear state, federation and evolution, retrieval-quality panel, **one-click memory dossier export** (Markdown, partitioned by epistemic origin, inferred entries marked "unverified").

## Integrations

| Host | How |
|---|---|
| Hermes Agent | MemoryProvider plugin: auto-save and auto-recall every turn (pre-compression rescue) |
| Claude Code | CLI hook / MCP |
| Cursor | Rules file (auto-saves to Raw Drawer on file save) |
| Any MCP client | MCP Server (41 tools, default :8766, stdio/HTTP dual transport) |
| Anything else | REST API (:8767) |

Details: [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md).

## MCP Server (41 tools · default port 8766)

MCP and REST share one process: REST on :8767, MCP on :8766 (stdio/HTTP dual transport). **Auth discipline**: a non-loopback bind requires `AIDUMEM_API_TOKEN` or the server refuses to start; only an explicit `AIDUMEM_ALLOW_INSECURE_PUBLIC=1` overrides (off by default, critical-logged when on). Tool groups and call examples: [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md).

## Security Model

Bearer token (`AIDUMEM_API_TOKEN`) + console password (PBKDF2) + injection guard + loopback by default. After startup, check Three probes first: `health_status`, `degraded`, and `probes.runtime_paths.data_dir_writable` on `/health` (without credentials the probes are redacted with a `_redacted` note). Three rounds of external security audits on record: [docs/SECURITY-AUDIT-LEDGER.md](docs/SECURITY-AUDIT-LEDGER.md).

## Key environment variables

| Variable | Purpose | Default |
|---|---|---|
| `AIDUMEM_API_TOKEN` | API auth token (mandatory for non-loopback) | empty = loopback only |
| `AIDUMEM_ENTITY_KEYWORDS` | Entity keywords (names/project codenames for the relevance gate) | empty |
| `AIDUMEM_DATA_DIR` | Data directory | `~/.aidumem` |
| `AIDUMEI_ENGINE_MODE` | Engine gear: cloud/auto/local | auto |
| `AIDUMEM_CONFIG_READONLY` | Read-only demo mode for console config | 0 |

The full registry lives in `ducky/env_registry.py` (code is the source of truth; typos trigger a startup warning).

## Capability map (one table, no stories)

| Layer | Capabilities |
|---|---|
| Retrieval | bge-m3 vectors + FTS5 CJK BM25/trigram + true cross-encoder rerank; relevance gate (chit-chat never triggers retrieval, saving tokens) |
| Memory semantics | Three-track decay (identity never decays / emotion accelerated / standard) · dual timeline (memories **expire**, not deleted) · six-type classification |
| Governance | Write-time dual review + conflict resolution + injection guard; full-path event ledger; cryptographic lineage (tamper-evident) |
| Evolution | Reflection (active/scheduled) · instinct→skill graduation (human approval gate) · retrieval self-evolution feedback loop |
| Collaboration | Federation: many agents share one memory base (MoE gate + fine-grained grants) |
| Extras | Multimodal visual memory · code graph · verbatim raw drawer · Obsidian interlinks |

## Testing & quality

**Test-layer honesty note (v19.4.1 P3-3)**
> How to read the table: in every row, passed + skipped equals the `pytest --collect-only` count **for that form on that date**; rows measured on different dates may have different denominators (the tree grows), so trust the date in each row. Skips are explained per axis (see [docs/TESTING.md](docs/TESTING.md)); they are not failures.

| Dimension | Status |
|---|---|
| Total cases | **2214** (measured via `pytest --collect-only`, 2026-09-23, f0.1 tree) = **2008 behavior + 70 script/hook + 136 guard** (split口径 `scripts/count_test_kinds.py`) |
| Clean dev machine | 2202 passed · **12 skipped** — **collected 2026-09-23** (f0.1 tree, Python 3.12; complete extras and model cache, only Hermes source absent) |
| Basic install path | 1821 passed · **25 skipped** — requirements files only, clean Python 3.12 venv (**measured 2026-09-09 on the production box**, v20.5a this tree) |
| Sandbox on the production box | 1967 passed · **26 skipped** — **measured 2026-09-11** (v20.5.1 this tree de09794, separate sandbox venv on the production box: host source present, no `.env`, optional axes absent); production host post-deploy: 1983 passed · 10 skipped (same tree, host axes present) |
| All axes present | 1844 passed · **1 skipped** — **measured 2026-09-09** (v20.5a this tree, separate all-axes venv on the production box; the 1 skip is a per-axis conditional from a new test on this tree) |
| Layering | Mostly module-level unit tests plus source-level guard assertions, with `TestClient`-driven API tests in support |
| Platform premise | The suite is maintained for Linux/macOS (POSIX): the `backup_gate` axis needs a POSIX shell; `/health` CPU/RSS metrics use the `resource` module and honestly report `None` on non-POSIX platforms. Windows is not a full-suite platform |
| Statement coverage | ~51% over `ducky/` and entry points |
| External coverage | Real mem0/Qdrant, model calls and recovery drills are production smoke tests, not unit tests |

```bash
# Full regression
pytest tests/
# Compile check
python -m compileall ducky api_server.py mcp_server.py
```

> **Why report both 2202 and 1821**: the first is the 2026-09-23 collection count of the complete optional environment on this tree; the second is the 2026-09-09 clean-venv measurement of the basic install path (requirements files only). A number only means anything with its environment and date attached (the basic-path figure is refreshed at the v21.1 production re-measurement).

> **The 12 skips are falsifiable, reproduce them yourself**: all thirteen skip axes (host, tools, optional deps, model files) are registered in [docs/TESTING.md](docs/TESTING.md); `HERMES_SRC` is tri-state controllable, reproducible in both directions:
>
> ```bash
> # Measured 2026-09-14: install all dependencies, deploy the model cache, and point AIDUMEI_BENCH_DATA_DIR at a directory containing locomo10.json
> pip install -r requirements.txt -r requirements-dev.txt
> pip install "mcp>=1.0.0,<2" ruff nltk regex numpy fastembed
> python scripts/fetch_local_embed_model.py
> pytest tests/ -q -rs | tail -1                                 # no host: 2202 passed, 12 skipped
> HERMES_SRC=/path/to/hermes-agent pytest tests/ -q | tail -1    # with host: 2214 passed
> HERMES_SRC=none pytest tests/ -q -rs | tail -1                 # forced off: 2202 passed, 12 skipped
> ```
>
> `2214 passed` in the block above requires **all thirteen axes present**; the host is only one of them — don't read "install the host" as "all green".
>
> **Full skip-axis census** (gated counts reconciled against live measurement; any drift goes red):
>
> | Skip axis | Gated cases | Location |
> |---|---:|---|
> | Host Hermes source | 12 | `tests/test_hermes_plugin.py` |
> | git worktree | 1 | brand-policy baseline |
> | `scripts/backup_gate.sh` + POSIX shell | 8 | backup-gate tests |
> | `qdrant_client` installed | 1 | vector-bank contract |
> | LoCoMo dataset present | 1 | official whole-dataset scan |
> | `regex` installed | 1 | metric differential test |
> | `numpy` installed | 1 | metric differential test |
> | `nltk` installed | 13 | official stemming metrics |
> | `git` executable present | 6 | throwaway-repository oracle |
> | `mem0ai` installed | 20 | real patch-layer tests |
> | `fastembed` installed | 1 | real local-model fallback test; the configured model cache must also be present |
> | `ruff` installed | 3 | real-defect static rules |
> | `mcp` extra installed | 8 | MCP import-surface guards + auth-behavior + SSE transport cases + search session passthrough (v21.2 M2) |
>
> On the production box in an isolated sandbox (host source present, no `.env`), the bare command actually prints 1967 passed, 26 skipped (measured 2026-09-11, v20.5.1 tree `de09794`) — axes differ, so numbers only travel with their environment and date.

## Security & compliance

MIT License. `SECURITY.md` + [docs/SECURITY-AUDIT-LEDGER.md](docs/SECURITY-AUDIT-LEDGER.md): multiple rounds of external security audits on record, including our reasons for rejecting false positives. The MCP layer refuses to start on non-loopback binds without a token.

## Known boundaries (honestly stated)

- Embedding and LLM services are required (cloud or the local spare) — that buys real semantic retrieval and extraction quality; for fully-offline sub-millisecond minimalism, zero-dependency local tools fit better, and we say so plainly.
- Benchmark protocols (LoCoMo / LongMemEval) are frozen; formal scores pending — honestly registered in [benchmarks/RESULTS.md](benchmarks/RESULTS.md).
- The local lite gear's recall overlap with the cloud gear is measured and limited (all differences priced openly in docs) — that is exactly why the auto gear exists.

## Docs

Deployment & operations: [AGENTS.md](AGENTS.md) (single agent entry point) · [docs/HEALTH.md](docs/HEALTH.md) · [docs/OPERATIONS.md](docs/OPERATIONS.md) · [TROUBLESHOOTING.md](TROUBLESHOOTING.md) · [docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md) · [docs/POSITIONING.md](docs/POSITIONING.md) (comparisons with alternatives, all figures reproducible)

## Known Limitations & Not Covered

The `(user_id, bank_id)` scope contract covers the **online read/write paths**. The three areas below are
**explicitly not covered** in this release. They are documented here rather than left for you to discover in production:

| # | Exception | Current state | Why not in this release |
|---|-----------|---------------|-------------------------|
| 1 | **`core_memory` key shape** | The table's primary key is still the single column `block_key` (`ducky/core_memory.py`). Isolation is enforced by the unique index `idx_core_memory_scope_key(user_id, bank_id, block_key_raw)` together with a write path whose `DO UPDATE SET` clause never touches the ownership columns | Changing the primary key shape is a **breaking** change and must come **after** existing rows have been reconciled to their true banks. Doing it in the other order would weld unreconciled data to the wrong bank |
| 2 | **Whole-database maintenance jobs** | Memory evolution and salience maintenance (`ducky/evolve_mem.py`, `ducky/routes_evolve.py`) scan the **whole database and do not isolate by bank**; this is annotated in the source docstrings | Whole-database maintenance is precisely their semantics — partitioning by bank would rob decay and consolidation of their global view. These jobs **never feed the user-visible retrieval path** |
| 3 | **Bank attribution of pre-existing data** | Memories carried over from v19 all land in the `default` bank and have **not** been reconciled to their true owners | The premise of an additive migration is that not one existing row is changed or deleted. True attribution requires business-side confirmation: that is data governance, not a code release |


## Repository layout

```text
aiduMEI/
├── AGENTS.md / llms.txt    # Agent deployment entry & docs index
├── api_server.py           # Main entry (API + /ui console hosting)
├── ducky/                  # Business logic (hot/ pipeline/ speed/ salience/ federation/ evolve_mem.py …)
├── integrations/           # Hermes plugin / Cursor hook / shell hooks
├── scripts/                # Deployment / smoke / backup / audit tooling
├── tests/                  # 2203+ test cases (behavior + script + guard)
└── benchmarks/             # Evaluation protocol (frozen, awaiting formal run)
```

## License

MIT — see [LICENSE](LICENSE).

<p align="center">
  <sub>aiduMEI ⚕ YouiSi (formerly aiduMEM / duMem — legacy names preserved in historical versions and docs) | Powered by monkey²</sub>
</p>
