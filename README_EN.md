<p align="center">
  <img src="assets/aidumei-v20-banner.svg" alt="aiduMEI v20.5" width="100%">
</p>

# 🤔 aiduMEI — AI Wisdom Engine

> **Not just memory — thinking.**
>
> *Optimization is not refactoring code, but implanting excellent logic;*
> *Memory is not note-taking, but never forgetting the details of the past;*
> *Thinking is not reasoning, but doing everything with reason and result.*

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.10–3.12](https://img.shields.io/badge/python-3.10–3.12-yellow.svg)](https://www.python.org/)
[![Built on mem0](https://img.shields.io/badge/built%20on-mem0-orange.svg)](https://github.com/mem0ai/mem0)

**[📖 中文文档](README.md)** | **English**

---

## 🎯 One-Line Prompt, Fully Automated Deployment

Send this single line to your AI Agent (**the canonical deployment text lives in [prompts/install.txt](prompts/install.txt) — the 13-step version; the divergence between this display zone and the canonical file was an audit finding, now converged and enforced by an acceptance hash check**):

```text
Install aiduMEI from the official repo strictly following AGENTS.md: deploy in its 11 steps, judge every step by script exit codes and JSON evidence only, stop-and-fix on failure, and finish with a machine-readable report.py summary.
```

> This line is the **entry pointer**; the deployment canon = [prompts/install.txt](prompts/install.txt) (copy its full text to your Agent). Details: [AGENTS.md](AGENTS.md).

---

## What is aiduMEI?

aiduMEI is an **AI Wisdom Engine** — a persistent memory and reasoning system for AI Agents. The current public release is **v20.5.0 (formal)** — **YouiSi: One-Line Prompt deployment, dual-engine autoshift, first of its kind.** All three preview-round review findings are closed: both commitment-level defects fixed at the root, all six yellows cleared (see CHANGELOG v20.5.0). The Wisdom Engine Autoshift provides a dual engine with automatic gear shifting. When external services fail it downshifts automatically and keeps running seamlessly; when they recover it upshifts and replays the debt; the gear is always honestly visible. v20.1's "deterministic fallbacks and honest recall" (17 remediation items closed across five external reviews) is its foundation. It embodies a complete **cognitive architecture** that enables AI to **remember, think, and evolve**.

<!-- distribution-policy: github-source-only -->
> **Distribution (GitHub-only):** aiduMEI no longer publishes or maintains packages on PyPI or GHCR. Get ongoing updates from the repository's `main` branch or formal versions from [GitHub Releases](https://github.com/monkey2jack/aiduMEI/releases). The `pip install -r requirements.txt` command below installs dependencies from a cloned source tree; it is not a package distribution method.

Built on top of [mem0](https://github.com/mem0ai/mem0), aiduMEI adds a version-by-version cognitive framework:

| Layer | Codename | What it does | Key Feature |
|-------|----------|-------------|-------------|
| 🦉 **Wisdom** | Athena | How to grow wiser after remembering | Active Reflect · memory self-editing · recursive refinement · skill growth · persona memory layer |
| 🧠 **Recall** | Mnemosyne | Find the right memory at the right time | Ebbinghaus decay + BM25/trigram + vector hybrid search |
| 🔍 **Gate** | Tahoe-Gate | Only retrieve what's actually relevant | Heuristic gate (`GET /gate`) blocks irrelevant context — casual chat skips retrieval, saving tokens & compute |
| 🌊 **Tidal** | Mnemosyne Tidal | Batch LLM extraction, not one-by-one | Async coalescing queue: multiple short messages → single LLM call |
| ⏳ **Decay** | Ebbinghaus | Forgetting is a feature, not a bug | Three-lane decay: Identity zero-decay / Emotion accelerated / General standard curve |
| 🕰️ **Chronos** | Chronos | Time-aware validity | Dual timeline (valid_from / valid_to), deprioritize without deletion |
| 🏛️ **Pantheon** | Pantheon | Many agents, one memory | Federated identity + MoE gating + 4-tier graceful degradation |
| 🛡️ **Aegis** | Aegis | Zero hardcoding, clone and run | Identity / paths / keywords all injected via env vars |
| 🌈 **Iris** | Iris | Rides the host's native memory channel | Hermes MemoryProvider plugin: pre-compress rescue · memory mirroring · direct tools |
| 🐙 **Octopod** | Opus Octopod | Memory governance & crystallization | ConflictResolver + TreeMemory + SkillCrystallizer |
| ⚡ **Zeus** | Zeus | King of the Gods | Raw Drawer + Code Graph + EvolveMem self-evolving retrieval + **multimodal vision memory · Obsidian bi-directional links · lossless fast-update** |

---

## 🚗 Wisdom Engine Autoshift (official in v20.3)

**One engine, three gears, automatic shifting** — the first open-source memory system to turn a
"local fallback" into full-pipeline automatic downshifting. But we don't force it on you:
**pick your gear, and we'll tell you exactly what it costs.**

### Pick your gear

The gear is **your deployment decision**, not an assumption we make for you. One line in `.env`:

| | ☁️ Cloud `cloud` | ⚙️ Autoshift `auto` (default) | 🔋 Local `local` |
|---|---|---|---|
| **Semantic recall** | full cloud embeddings | cloud-first, **auto-fails over to local** | local ONNX only (512-dim) |
| **Memory distillation** | LLM distillation | LLM, falls back to deterministic | deterministic only, **no LLM** |
| **When the service dies** | no spare tire; reports `degraded` | **keeps running**, upshifts on recovery | nothing external to lose |
| **Token spend** | normal | normal (zero while downshifted) | **always zero** |
| **API keys** | required | required (without them it stays local) | **none at all** |
| **How to switch** | `AIDUMEI_ENGINE_MODE=cloud` | default, or `=auto` | `=local` |

All three share **the same data and the same contract** — switching gears migrates nothing,
rebuilds no index, and changes no caller code. The two legs (cloud / local) are **independent
switches** rather than a rigid three-way branch: adding a fourth shape later would not touch a
single call site.

### We tell you exactly what it costs

"Saving memory" and "letting you choose" are **the same thing** here, because we measured first:

| | Cloud gear | Autoshift / Local gear | Difference |
|---|---|---|---|
| Resident memory | **~280 MB** | ~430 MB | **150 MB** |
| Dependency disk | ~275 MB | ~353 MB + 91 MB model | ~169 MB |

And we account for those 150 MB out loud: **onnxruntime itself costs about 75 MB just to import, and the
model session and weights about 122 MB.** (The components are measured separately and total ~197 MB;
that is a different yardstick from the 150 MB two-gear resident difference — the cloud gear carries
baseline overhead too, and components share pages. We list both honestly instead of forcing the sum.)
We tried to shrink it — `threads=1`, on-demand ONNX arena
allocation, `malloc_trim`, `MALLOC_ARENA_MAX=2` — and **all four knobs measured as no-ops**
(206–215 MB, within noise); the model is already the smallest usable Chinese-capable option.
So we **didn't pretend to optimize — we made it a switch**: skip the spare tire and those 150 MB
cost you nothing.

> Why the spare tire stays resident rather than loading "only during an outage": the dual index
> computes a local vector on **every write**. Skip that and there is no local data — loading the
> model at the moment of an outage would recall nothing. **The spare tire is prepared in advance,
> not fetched on demand.** A deliberate trade-off, stated here so you can judge it yourself.
> Full measurements (cold start, latency, CPU) are in the Deployment Footprint section below.

### How the autoshift actually works

- **Auto downshift**: consecutive embedding failures trip a circuit breaker; and the fallback happens **within the same request** — when the cloud leg dies mid-query, that very query lands on the local index. Seamless, not "the next user gets the downgrade".
- **Auto upshift**: half-open probing uses real traffic; only consecutive successes shift back up (a lucky single success cannot fake a recovery). Distillation debt accrued while downshifted is replayed automatically — nothing lost, and a restart doesn't write it off either.
- **The LLM distillation leg has a gear too**: when distillation goes down, writes degrade to deterministic direct storage within seconds — verbatim text, hard facts and cloud vectors all land and stay recallable; only the polish is owed, and the debt is auditable. Transport-level blind retries are clipped (a gateway `Retry-After` can no longer hang a single write for minutes). Measured during an outage: a single write went from **4.5 minutes to 0.15 s**.
- **Honest gear**: `/search` responses carry `engine_mode` reported per the leg actually used this request, not per the system gear; lite scores are scale-annotated; every shift lands in the event ledger. `/health` exposes `engine_mode_policy`, `engine_gear` and `llm_gear`, and a leg switched off by configuration **reports `disabled_by_policy`** rather than pretending to be in service.
- **Production-proven, not a design doc**: the outage drill ran against the real production box — endpoints firewalled → auto downshift after three failures → writes during the outage land and are **semantically recallable** (`vector_leg=local`, verified) → auto upshift on recovery → debt drained to zero. Two real external gateway outages happened to hit during the drills; the machinery caught both.
- **Honest about the downside**: lite is a survival gear, not a drop-in equal — across 20 real queries, local-vs-cloud top-5 overlap is ~9% (measured 2026-08-26, 20 real queries, top-5 Jaccard overlap; the metric includes verbatim-vector dilution and small-vs-large model ranking divergence). **During an outage you find what must be found; ranking quality is explicitly below the cloud gear.**
- **Contract differences stated too**: a lite-gear `/add` acknowledgement is a write-path contract (`status` / `action` / `engine_mode`) and does **not** carry the `/search` recall-verdict field family — accepting debt is not recalling.

A bare install (no cloud keys) simply runs on the local gear forever — **a zero-dependency memory
library out of the box**; add keys and it upshifts automatically. One package, three ways to live
with it, your call.


## 🛡️ Audited boundaries and honest failure semantics

Independent reviews overturned three claims: local mode still had direct LLM call sites; some secondary endpoints ignored the two-dimensional scope; and the injection wrapper could be disabled by content containing `<memory>`. All verified findings were remediated with structural guards rather than per-call-site promises.

The resulting contract:

- `local` mode gates outbound model calls at their shared boundaries.
- Tenant scope is `(user_id, bank_id)` where the schema supports it; checkpoints, persona versions and user-only observations are explicit exceptions, surfaced by `delete_all.not_cleared`.
- Injected records use encoded, nonce-bearing boundaries rather than substring detection.
- Deletion is four-state: `committed`→200, `partial`→207, `failed`→500, `not_found`→200. `failed_layers` and declared exemptions stay separate; non-committed work remains replayable in WAL.
- A configured-but-unreachable vector backend is a real deletion failure. Only the typed initialization signal for a never-configured backend may skip the mem0 leg.
- Runtime paths are handed over explicitly by Docker/systemd and reported by `/health`.
- Static guards fail or skip honestly when their dependency is absent; they never report "zero findings" when no scan ran.

Community Issue #5 also exposed a recall contradiction: a batch could be labelled `not_found` while its weak candidates were still returned. The scoring exit now drops zero-evidence items and applies the deployment's calibrated verdict threshold as the recall floor unless explicitly overridden. See `CHANGELOG.md` and `docs/TESTING.md` for measurements and audit history.


## Architecture contract and trade-offs

v20 changed memory ownership from a channel convention into an explicit `(user_id, bank_id)` contract across online write, query, delete, restore, feedback and ledger paths. Migration is additive: existing rows are not rewritten, and unreconciled legacy rows remain in the `default` bank.

The architecture deliberately trades a tiny local footprint for governance and resilience:

- Cloud embedding and LLM legs provide higher-quality semantic recall and distillation.
- The local ONNX leg keeps writes and recall available when those services fail.
- SQLite/FTS5 is the durable floor; Qdrant carries semantic vectors.
- Human-reviewable governance, event ledgers and four-state deletion favor evidence over a one-bit "ok".
- Multi-tenant here means memory ownership inside one **single-machine self-hosted** deployment, not a SaaS security boundary. Run separate instances for mutually untrusted parties.

The current release claims no benchmark score. `benchmarks/` pins dataset, model, judge, prompts, seeds and hashes, and drives the public HTTP contract so future numbers can ship with their reproduction record.

---

## Install and verify

The canonical agent deployment contract is [prompts/install.txt](prompts/install.txt); [AGENTS.md](AGENTS.md) explains the same steps, evidence fields, recovery and host integration. The one-line prompt at the top is only a pointer to that canon.

<details>
<summary>Manual source installation</summary>

```bash
git clone https://github.com/monkey2jack/aiduMEI.git && cd aiduMEI
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp mem0_config_local.json.example mem0_config_local.json
cp .env.example .env
python api_server.py
python scripts/e2e_smoke.py --json
python scripts/report.py --json
```

For the local spare tire, install `.[local-embed]` and run `python scripts/fetch_local_embed_model.py` before starting the service. Fetched model files are verified file-by-file against the sha256 manifest `scripts/local_embed_model_sha256.json` — a mismatch is deleted and the script exits non-zero.
</details>

Choose one mode in `.env`:

```bash
AIDUMEI_ENGINE_MODE=auto    # default: cloud-first, automatic fallback and recovery
AIDUMEI_ENGINE_MODE=cloud   # lean: cloud only; local model is never loaded
AIDUMEI_ENGINE_MODE=local   # zero model API calls; deterministic + local embedding
```

### Containers and Dockhold

The 2026-09-07 `main` maintenance update adds container-hosting support (that update kept the public version and Release at v20.3). Port priority is `AIDUMEM_API_PORT → MEM0_API_PORT → PORT → 8767`; leave the first two unset to use the platform's injected `PORT`. The image provides a writable HOME and supports platform-assigned UIDs in group 0.

Persist the data, model configuration, Qdrant vectors and history database. Setting `AIDUMEM_DATA_DIR` alone does not relocate the last two. Follow the [Dockhold deployment guide](docs/DEPLOY_DOCKHOLD.md) for credentials, storage and recall checks across container replacement. Thanks to [Maziar110](https://github.com/Maziar110) for [container fixes #10](https://github.com/monkey2jack/aiduMEI/pull/10) and [the deployment guide #11](https://github.com/monkey2jack/aiduMEI/pull/11).

### Measured deployment footprint

Measured on a 2-core / 3.5 GB host on 2026-08-27:

| | Cloud | Auto / Local |
|---|---:|---:|
| Resident memory | ~280 MB | ~430 MB |
| Dependencies | ~275 MB | ~353 MB + 91 MB model |
| Suggested host | 1 core / 1 GB | 2 cores / 2 GB |

The ~150 MB resident difference is the prepared local vector leg: it must index every write before an outage, so loading it only after failure would leave nothing local to recall. Full operations and capacity details: [docs/OPERATIONS.md](docs/OPERATIONS.md) · [docs/CAPACITY.md](docs/CAPACITY.md).

---

## 🔐 Security Model

**One gate, two keys.** Both are accepted; either one grants access:

| Key | Who uses it | How |
|-----|-------------|-----|
| Session cookie | Browser console | `POST /login` with the console password; the server issues an HttpOnly, SameSite=Lax session cookie |
| Bearer token | Scripts, MCP, CI | `Authorization: Bearer <AIDUMEM_API_TOKEN>` |

The gate activates when **either** `AIDUMEM_API_TOKEN` is set **or** the console password is set explicitly
(via env var, or by changing it through the console). A password auto-generated at first boot guards the console
login only — it deliberately does **not** activate the REST gate, so existing loopback callers (Hermes plugin,
MCP, cron) keep working across an upgrade. Check `probes.auth_gate_enabled` in `/health` to see the current state.

- **The service binds `127.0.0.1` by default.** A credential-less instance refuses to start on a non-loopback
  bind (explicit escape valves: `AIDUMEI_TRUST_PROXY=1` to declare a trusted proxy,
  `AIDUMEM_ALLOW_INSECURE_PUBLIC=1` to allow credential-less public exposure — both off by default).
  For cross-machine access, configure a credential and put a TLS reverse proxy in front; never expose a
  credential-less instance to the public internet.
- **Tenant scoping is not a SaaS security boundary.** `(user_id, bank_id)` separates memory ownership among
  agents/identities inside one single-machine self-hosted deployment; `AIDUMEM_STRICT_TENANT=1` switches to
  strict mode (no fallback for unlabeled historical rows). Host mutually untrusted parties as
  separate instances.
- **Sessions are in-process**: a restart invalidates all login sessions, and sessions are not shared across
  instances — put sticky sessions on the proxy for multi-instance deployments.
- **Passwords** are stored as PBKDF2-HMAC-SHA256 (200k rounds) in `data/.ui_password_hash` with mode 0600;
  pre-v19.4.1 single-round SHA-256 hashes are upgraded automatically on first successful login.
- **MCP shares REST's credential**: all 41 MCP tools land their writes through the REST API, and a
  non-loopback SSE bind requires `AIDUMEM_API_TOKEN` (see the integration section below).

## 🩺 Three probes to check first

```bash
curl -s -H "Authorization: Bearer $AIDUMEM_API_TOKEN" http://127.0.0.1:8767/health | jq '.health_status, .degraded, .probes.runtime_paths'
```

1. `health_status` must be `ok`;
2. every item in `degraded` must have an explanation in `degraded_details`;
3. `probes.runtime_paths.data_dir` must be the directory you intend to persist, with `data_dir_writable: true`.

Probes are graded by cost (since v20.4.0): `/livez` (O(1) liveness), `/readyz` (cheap readiness checks,
503 takes the instance out of rotation), `/diagnostics` (full deep probe, credential required); the `/health`
contract is unchanged. Field semantics: [docs/HEALTH.md](docs/HEALTH.md). **`/health: ok` alone does not prove
memory works** — run the write→recall→cleanup smoke: `python scripts/e2e_smoke.py --json`.

---

## Architecture

```text
Host agent native short-term memory
              │
              ▼
FastAPI / MCP contract ── relevance gate ── recall funnel
       │                         │
       ├─ durable facts + FTS5   ├─ cloud vector index
       ├─ verbatim vault         └─ local ONNX index
       ├─ workspace/core memory
       └─ WAL + governance + evolution ledgers
```

The host owns short-term conversation state; aiduMEI owns durable long-term memory. Cloud and local vector legs are independent, while deterministic extraction and FTS remain available without model credentials. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (v14-era historical snapshot, archived under docs/ since v20.4.1) for module boundaries and [docs/HEALTH.md](docs/HEALTH.md) for probe semantics.

Key capabilities include relevance-gated recall, tidal write coalescing, time-aware decay, verbatim storage, code-impact analysis, feedback-driven retrieval, persona memory, conflict resolution, skill crystallization and multi-agent federation. The release history belongs in [CHANGELOG.md](CHANGELOG.md), not in this deployment entry page.

---

## Core API contracts

| Method | Path | Contract |
|---|---|---|
| `POST` | `/add` | Distilled or deterministic durable write; `action` states direct/async/coalesce behavior |
| `POST` | `/add/raw` | Zero-LLM verbatim write |
| `POST` | `/search` | Hybrid recall with verdict, confidence, engine mode, and trace fields |
| `POST` | `/search_trace` | Recall with funnel evidence |
| `DELETE` / `POST` | `/delete` | Scoped idempotent single deletion |
| `POST` | `/delete_all` | Confirmed scoped purge |
| `GET` | `/health` | Probe details, active modes and actual runtime paths |
| `GET` | `/gate` | Decide whether a turn needs memory retrieval |
| `GET` | `/stats` | Scope-scoped memory statistics |
| `POST` | `/api/core-memory/inject` | Inject core memory context |
| `POST` | `/facts/inject-context` | Inject fact context |
| `POST` | `/session/start` / `/session/end` | Session lifecycle |
| `GET` | `/metrics` | JSON metrics (not Prometheus format) |

Deletion outcomes are `committed`→200, `partial`→207, `failed`→500 and `not_found`→200. `failed_layers` reports failures on this call; `not_cleared` reports declared matrix exemptions. A configured-but-unreachable vector backend is a failure. Only the typed initialization signal for a never-configured backend may skip the mem0 leg, and non-committed WAL work stays replayable.

**Async-write consistency window** (explicit since v20.4.1b): when `/add` returns `coalesce_buffered`, the raw record and FTS index are searchable immediately, while the vector leg is completed by the background coalesce queue. Until it lands, the memory is reachable via keyword recall but its vector score is still 0, so ranking may show `score=0.000`. Window length is governed by the `AIDUMEI_COALESCE_*` knobs; the truthful progress signal is `/health`'s WAL and coalesce fields, not wall-clock time.

**Cold-start ranking of fresh writes** (explicit since v20.4.1b): salience accrues from recall hits, so a just-written memory starts at 0 and ranks below older, proven records — an honest cold start, not a recall failure. Fresh probe terms are findable within seconds via the keyword leg (FTS substring/prefix), not via ranking score. Ruling for v20.4.1b: no scoring change ships in a patch release; the "freshness annotation on recall results" evaluation moves to the v21 track.

`/search` bounds `limit` to `1..100`; an empty query returns `recall_verdict="empty_query"`. Zero-evidence candidates are dropped, while `AIDUMEI_RECALL_MIN_HYBRID=0.0` leaves the composite floor disabled unless the deployment calibrates one.

Interactive API documentation is served at `/docs`. Extended endpoint groups and request shapes are documented by the live OpenAPI schema; operational scripts must judge the HTTP status and JSON body together.

---

## Agent and tool integration

The recommended host path is the Hermes MemoryProvider plugin: it injects durable context at turn start, archives in the background, rescues context before compression and mirrors native long-term-memory writes. A shell hook is available when a host cannot load plugins. **Do not enable both**, because duplicate injection wastes context. Follow [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md) and [integrations/INTEGRATION_GUIDE.md](integrations/INTEGRATION_GUIDE.md) for verification and rollback.

The MCP server listens on `:8766` and exposes **41 tools**. Run `python mcp_server.py --help` for transports and flags.

> **Auth policy (stated explicitly since v20.4.0):** MCP tool writes land through the REST API and share its
> credential. stdio and SSE bound to loopback (`127.0.0.1`/`localhost`/`::1`) trust the local machine and need
> no credential; **SSE bound to a non-loopback address must configure `AIDUMEM_API_TOKEN`** (injected as Bearer
> at startup) — without one the server refuses to start, so the 41 tools cannot become a backdoor around REST
> auth. For deliberate credential-less exposure, the explicit escape valve is `AIDUMEM_ALLOW_INSECURE_PUBLIC=1`
> (same name and behavior as the REST public valve; off by default, logs at critical level when enabled).

| Tool group | Count | Notes |
|--------|------|------|
| Core CRUD | 6 | add / search / delete / update / recent / stats |
| Facts | 4 | facts_add / facts_search / facts_list / facts_delete |
| Code Graph | 2 | code_impact / code_graph |
| Session | 2 | session_list / session_history |
| Reflect | 2 | reflect_recent / reflect_trace |
| Core Memory | 3 | core_memory_get / core_memory_set / core_memory_list |
| AutoDream | 2 | dream_trigger / dream_status |
| Raw Drawer | 2 | raw_add / raw_search |
| Knowledge Tree | 3 | tree_nodes / tree_node / tree_ancestors |
| Crystals | 3 | crystals_list / crystals_detect / crystals_approve |
| Conflict | 1 | conflict_resolve |
| Evolve | 2 | evolve_feedback / evolve_report |
| Federation | 6 | fed_recall / fed_add / fed_agents / fed_register / fed_broadcast / fed_awareness |
| Persona | 3 | persona_build / persona_retrieve / persona_banks |

IDE adapters live under `integrations/`; they call the same API rather than maintaining a second memory implementation.

```bash
# Cursor: copy the rules file into your project
cp integrations/cursor-hook/cursor-aidumem.mdc .cursor/rules/
# Claude Code hook: store / search / impact from the shell
python integrations/cursor-hook/claude-code-hook.py store --file my_code.py
```

---

## Tech Stack

- **Runtime**: Python 3.10–3.12 (3.12 recommended), FastAPI, Uvicorn
- **Memory Kernel**: mem0ai v2.0.20
- **Vector Store**: Qdrant (via qdrant-client)
- **Structured Data**: SQLite (facts.db, observations.db, scenes.db, fact_events.db)
- **Full-Text Search**: SQLite FTS5 + trigram tokenizer. Chinese queries shorter than 3 characters fall back to `LIKE` (a tokenizer definition, not a defect); the `_recall_path` field (`fts` / `like`) records which path each recall actually took
- **Embeddings**: Configurable (OpenAI Embedding API compatible)
- **Reranking**: Configurable (OpenAI Rerank API compatible)
- **LLM**: Any OpenAI-compatible API
- **MCP**: fastmcp stdio + HTTP dual-mode

---

## Configuration

Copy `mem0_config_local.json.example` to `mem0_config_local.json` and edit the nested `llm.config`, `embedder.config`, `vector_store.config` and optional `rerank.config` sections. The shipped example is the schema reference; keeping a second JSON copy here would let the two drift. Use `GET /health` to confirm the paths and backend state actually in effect.
Since v20.4.0, the `AIDUMEI_LLM_API_KEY` / `AIDUMEI_EMBEDDER_API_KEY` / `AIDUMEI_RERANKER_API_KEY` environment variables override the JSON `api_key` values, so secrets never have to touch the file.

---

## Environment Variables

Since v14 Aegis, all deployment-specific settings are injected via environment variables — **all optional**, safe defaults when unset. The deployment-critical rows:

| Variable | Default | Description |
|----------|---------|-------------|
| `AIDUMEM_DATA_DIR` | `<repo>/data` | Database & vector store location |
| `AIDUMEM_API_TOKEN` | empty | REST API token. Once set, **every** endpoint requires `Authorization: Bearer`. Optional on loopback; **mandatory for any deployment reachable from outside** |
| `AIDUMEM_API_PORT` | `8767` | API + console listen port. Falls back to `MEM0_API_PORT`, then to `PORT` (the standard name container PaaS platforms use to inject a runtime port) |
| `AIDUMEM_ENTITY_KEYWORDS` | empty | Custom entity keywords for relevance gate, `\|` separated |
| `AIDUMEM_LEGACY_USER_IDS` | empty | Historical `user_id` aliases (comma-separated); without the mapping, older rows cannot be recalled |
| `AIDUMEM_CONFIG_READONLY` | `0` | `1` makes the console's config endpoints read-only |

Full list (host integration, federation, log directory and more) with comments: [`.env.example`](.env.example). Start with `cp .env.example .env`.

---

## Testing & quality

All five rows — Total cases, Clean dev machine, Basic install path, Sandbox and All axes present — are **measured on this tree (v20.4.1) on 2026-09-09** (the latter three on the production box). Historical validation provenance: [docs/TESTING.md](docs/TESTING.md#2026-09-07-公开维护与验证来源).

```bash
# Complete environment: no-host result measured on this tree, 2026-09-08
# Configure AIDUMEI_LOCAL_EMBED_CACHE and AIDUMEI_BENCH_DATA_DIR (containing locomo10.json)
# Deploy the model before testing; runtime is offline-only
pip install -r requirements.txt -r requirements-dev.txt
pip install "mcp>=1.0.0,<2" ruff nltk regex numpy fastembed
python scripts/fetch_local_embed_model.py
pytest tests/ -q -rs | tail -1                                 # no host: 1876 passed, 12 skipped
HERMES_SRC=/path/to/hermes-agent pytest tests/ -q | tail -1    # with host: 1888 passed
HERMES_SRC=none pytest tests/ -q -rs | tail -1                 # forced off: 1876 passed, 12 skipped

# Basic source-install path: use a separate clean venv; measured 2026-09-08
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -q -rs | tail -1                                 # basic path: 1777 passed, 35 skipped
```

> How to read the table: in every row, passed + skipped equals the `pytest --collect-only` count **for that form on that date**; rows measured on different dates may have different denominators (the tree grows), so trust the date in each row. Skips are explained per axis (table below); they are not failures.

| Dimension | Status |
|---|---|
| Total cases | **1888** (measured via `pytest --collect-only`, 2026-09-10, v20.5.0 formal-remediation tree) |
| Clean dev machine | 1876 passed · **12 skipped** — **measured 2026-09-10** (v20.5.0 formal-remediation tree, Python 3.12; complete extras and model cache, only Hermes source absent) |
| Basic install path | 1821 passed · **25 skipped** — requirements files only, clean Python 3.12 venv (**measured 2026-09-09 on the production box**, v20.5a this tree) |
| Sandbox on the production box | 1877 passed · **11 skipped** — **measured 2026-09-10** (v20.5.0 formal this tree 6781c7d, separate sandbox venv on the production box: host source present, no `.env`, without ruff/mcp/fastembed; the 11 are `ruff` ×3, `mcp` ×7 and an empty standby-model cache ×1) |
| All axes present | 1844 passed · **1 skipped** — **measured 2026-09-09** (v20.5a this tree, separate all-axes venv on the production box; the 1 skip is a per-axis conditional from a new test on this tree) |
| Statement coverage | ~51% over `ducky/` and entry points |
| Test layering | Mostly module-level unit tests plus source-level guard assertions, with `TestClient`-driven API tests in support |
| Platform premise | The suite is maintained for Linux/macOS (POSIX): the `backup_gate` axis needs a POSIX shell; `/health` CPU/RSS metrics use the `resource` module and honestly report `None` on non-POSIX platforms. Windows is not a full-suite platform |
| External coverage | Real mem0/Qdrant, model calls and recovery drills are production smoke tests, not unit tests |

**Why report both 1876 and 1821**: the first is the 2026-09-10 measurement of the complete optional environment on this tree; the second is the 2026-09-09 clean-venv measurement of the basic install path (requirements files only, this tree). With the complete environment, model cache and public LoCoMo dataset present, both host states produce:

```text
no host: 1876 passed, 12 skipped
with host: 1888 passed
forced off: 1876 passed, 12 skipped
```

On a production host where other optional axes are absent, the bare command **actually prints 1877 passed, 11 skipped** (measured 2026-09-10 on the v20.5.0 formal tree 6781c7d in an isolated sandbox on the production box, no `.env`, isolated HOME, public dataset reused read-only; the 11 are `ruff` ×3, `mcp` ×7 and an empty standby-model cache ×1). A number without its environment and date is not a reproducible claim.

### Skip-axis census

| Skip axis | Gated cases | Location |
|---|---:|---|
| Host Hermes source | 12 | `tests/test_hermes_plugin.py` |
| git worktree | 1 | brand-policy baseline |
| `scripts/backup_gate.sh` + POSIX shell | 8 | backup-gate tests |
| `qdrant_client` installed | 1 | vector-bank contract |
| LoCoMo dataset present | 1 | official whole-dataset scan |
| `regex` installed | 1 | metric differential test |
| `numpy` installed | 1 | metric differential test |
| `nltk` installed | 13 | official stemming metrics |
| `git` executable present | 6 | throwaway-repository oracle |
| `mem0ai` installed | 20 | real patch-layer tests |
| `fastembed` installed | 1 | real local-model fallback test; the configured model cache must also be present |
| `ruff` installed | 3 | real-defect static rules |
| `mcp` extra installed | 7 | MCP import-surface guards + auth-behavior + SSE transport cases |

The suite is maintained for Linux/macOS POSIX. Guards that lack their tool skip honestly instead of reporting zero findings. Every payload-, credential- or response-shape fix needs its production shape plus a discriminating negative control; named tests must be PASSED, not silently SKIPPED.

---

## Known Limitations & Not Covered

The `(user_id, bank_id)` scope contract covers the **online read/write paths**. The three areas below are
**explicitly not covered** in this release. They are documented here rather than left for you to discover in production:

| # | Exception | Current state | Why not in this release |
|---|-----------|---------------|-------------------------|
| 1 | **`core_memory` key shape** | The table's primary key is still the single column `block_key` (`ducky/core_memory.py`). Isolation is enforced by the unique index `idx_core_memory_scope_key(user_id, bank_id, block_key_raw)` together with a write path whose `DO UPDATE SET` clause never touches the ownership columns | Changing the primary key shape is a **breaking** change and must come **after** existing rows have been reconciled to their true banks. Doing it in the other order would weld unreconciled data to the wrong bank |
| 2 | **Whole-database maintenance jobs** | Memory evolution and salience maintenance (`ducky/evolve_mem.py`, `ducky/routes_evolve.py`) scan the **whole database and do not isolate by bank**; this is annotated in the source docstrings | Whole-database maintenance is precisely their semantics — partitioning by bank would rob decay and consolidation of their global view. These jobs **never feed the user-visible retrieval path** |
| 3 | **Bank attribution of pre-existing data** | Memories carried over from v19 all land in the `default` bank and have **not** been reconciled to their true owners | The premise of an additive migration is that not one existing row is changed or deleted. True attribution requires business-side confirmation: that is data governance, not a code release |

**The boundary of the boundary**: exceptions 1 and 2 cannot cause one bank to read another's data — 1 is backstopped by
the unique index (writes never rewrite ownership columns), and 2 never enters the retrieval path. The impact of
exception 3 is "the bank label is inaccurate", not "banks leak into each other".

> This section is itself a discipline: whenever the README states isolation in absolute terms, a **list of known
> exceptions** must ship alongside it. Claiming "full" coverage without listing the exceptions leaves the user to
> find the boundary in production — the exact failure shape this project has paid for repeatedly.

---

## aiduMEI Console

The backend serves a build-free console at `/ui`:

- **PULSE** — service, storage and per-module probe state.
- **VAULT** — scoped search, categories and recent facts.
- **MAP** — knowledge graph.
- **RECALL** — candidate-to-final funnel with latency and drop evidence.
- **EVOLVE** — retrieval quality and feedback history.
- **SETTINGS** — read-only, masked model configuration and federation state.

The repository intentionally ships no screenshots: start the service and verify the live console rather than trusting stale demo data.

---

## Version history

Major-version architecture and remediation history is maintained in [CHANGELOG.md](CHANGELOG.md). Version identifiers from v20 onward are plain numbers; historical codenames are documentation landmarks, not runtime contracts.

---

<p align="center">
  <sub>AI Wisdom Engine | Built by the aiduMEI Team</sub>
</p>
