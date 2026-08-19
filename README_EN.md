<p align="center">
  <img src="assets/aidumem-banner.jpg" alt="aiduMEI" width="100%">
</p>

# 🤔 aiduMEI — AI Wisdom Engine

> **Not just memory — thinking.**
>
> *Optimization is not refactoring code, but implanting excellent logic;*
> *Memory is not note-taking, but never forgetting the details of the past;*
> *Thinking is not reasoning, but doing everything with reason and result.*

[![Docker Image](https://img.shields.io/badge/docker-ghcr.io-blue?logo=docker)](https://github.com/monkey2jack/aiduMEI/pkgs/container/aidumei)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-yellow.svg)](https://www.python.org/)
[![Built on mem0](https://img.shields.io/badge/built%20on-mem0-orange.svg)](https://github.com/mem0ai/mem0)

**[📖 中文文档](README.md)** | **English**

---

## What is aiduMEI?

aiduMEI is an **AI Wisdom Engine** — a persistent memory and reasoning system for AI Agents. Named after the Greek gods, it embodies a complete **cognitive architecture** that enables AI to **remember, think, and evolve**.

> **v19.4.2 · Athena — Guard Coverage & Integration Credential Wiring.** A close-out release for v19.4.1; no new features. The post-deploy re-audit (which included a Hermes Agent upgrade) found that the gate itself was built correctly, but **the list of "who needs a key" was incomplete**. v19.4.1 had shipped a guard test to stop callers from omitting credentials — except that guard scanned only `scripts/`, while the actual defects lived in the repo root, `integrations/`, and `mcp_server.py`. It caught none of them. **A guard whose range is smaller than the defect distribution is worse than no guard at all**, because it manufactures the illusion of protection. So the core of this release is not "fix a few more files" but **welding the guard's own range shut with a meta-test**: it asserts that the guard's coverage set ⊇ the set of every file in the repo that actually issues an HTTP request to this service. Narrow the range and it goes red immediately. That meta-test caught two entry points I had miscounted on its very first run, and a third after the widening — the plan named 5, reality had **9**. Credential wiring now covers 8 entry points (the Hermes injection hook, `mem0_sync`/`seed_demo`/`seed_facts`, `mcp_server`, the Cursor on-save hook, the Claude Code hook, and the Hermes plugin), all converged onto the single source of truth `ducky.utils.api_auth_headers()` and one shared `.env` fallback chain. The Hermes plugin is the textbook **"looks fixed"** case: the code plainly carried an `Authorization` header, but read the token from environment variables only — and the gateway launches plugins with a nearly empty environment, so every request went out with an empty token. Three silent failures were also fixed: the sync unit lacked `StartLimitIntervalSec`, so a crashloop stays in `activating` forever and never reaches `failed` (monitoring that alerts on `failed` waits forever); logrotate's rename-based rotation silently vanishes logs for a `StandardOutput=append:` unit (now `copytruncate`); and the sync daemon's dependency had never been declared in any manifest. On branding, the wordmark residue left by v19.4.1 is cleared (the tag-split form `aidu<b>MEM</b>` is invisible to a global sed), and the hardcoded version string in `ducky/__init__.py` — two releases stale — is removed rather than merely reset. Two closing rounds followed the deploy. The **audit-remediation round** (user-perspective audit plus self-inspection during the fixes) sent back findings that all pointed at the same thing again: the guard written *in this very release* still had a range smaller than the defect distribution. `frontend/dev_server.py` escaped the scan twice over — by **directory** and by **signal** (directory-level exemptions are the fastest way to accumulate blind spots: the reason for the exemption expires as the directory grows things, while the exemption itself never does). The README number guard watched a single row of the Chinese table and let the prose on the same page — and the entire English file — walk free. The worst finding came from self-inspection: the `StartLimit*` keys "fixed" above had been written into the `[Service]` section, and systemd parses them **only in `[Unit]`** — in the file in plain sight, greppable, review-approved, and behaviourally identical to not having fixed it at all. **Configuration written is not configuration in effect**; the only valid acceptance test is to ask systemd for the value it computed. The **close-out round** supplies the other half of the "12 skipped" number: host auto-discovery hits `/hermes/hermes-agent`, so on any machine that has that tree (production does) the README's repro command prints `403 passed, 0 skipped` — the reader **cannot reproduce the skip count we claim**. **Reproducible in both directions is what makes it falsifiable**: a "skip" you cannot make skip is exactly as untrustworthy as a "pass" you cannot make pass. The root cause was a resolver that stuffed the environment variable and the hardcoded paths into **one ordered candidate list**, so `HERMES_SRC=/typo` fell **silently** through to the auto-discovered path — you point at A, you test B, and it stays green. **An implicit fallback will quietly overrule an explicit intent.** It is now three-state, with **explicit always beating implicit**: unset → auto-discovery; `none/no/off/0/false/empty` → forced host-absent, not a single fallback attempted; explicit path invalid → raise, naming the bad path.

> **v19.4.1 · Athena — Audit Patch · Auth Unification & Tenant Closure.** No new features in this release; it closes the gaps between what the docs claimed and what the code actually did. The audit method shifted from line-by-line review to **probe-driven verification**: for every claim in the README, write a minimal runnable program that tries to *disprove* it. Four claims fell and were fixed one by one: **auth unification** (previously setting only the UI password left every endpoint wide open, while setting only the API token bricked the console — now it is one gate with two keys: login issues an HttpOnly session cookie, and either that or a Bearer token grants access), **tenant closure** (visibility scoping now covers the facts layer, plus a fix for silent cross-tenant overwrites where two tenants writing the same key would destroy each other's value), **deletion rights honored** (cascade delete now covers the Verbatim Vault; the verbatim handle returned by `/search` previously could not delete itself — a "searchable but undeletable orphan"), and **idempotency & index alignment** (the dedup key contained a timestamp so production payloads never collided; Chinese text was tokenized as 2-grams against a trigram index, meaning Chinese queries *never* hit the index and always fell back to full scans — 32.8ms → 0.05ms on 200k rows). Three "silent failure that covers its own tracks" cases were also fixed: a compatibility-facade gap that had killed the consolidation job silently for three weeks, two auxiliary stores whose cascade cleanup had *never* executed since introduction (leaving 252 ghost records that in turn made deletion logs look perfect while doing nothing), and a SkillCrystallizer SQL dialect error swallowed into a fake "no patterns found yet". The backup gate's "verification invalidates its own baseline" flaw is fixed too. 107 new tests, all following the **anti-false-green rule**: payload/credential/query shapes are tested across *every* shape, and index assertions verify `_recall_path` rather than merely counting hits.

> **v19.4.0 · Athena — Project Mirror Phase 1 · Verbatim Vault + Production Audit Fix Release.** Building on the v19.3 architectural unification, v19.4.0 opens "Project Mirror": using public memory-evaluation leaderboards as a mirror to see our own gaps — no competition, just craft. **Every word you said is kept** — the new Verbatim Vault stores verbatim conversation turns in parallel with mem0's fact extraction (per-tenant visibility scoping + idempotent dedup + trigram full-text index), and fuses verbatim evidence into recall results, so memory keeps not only the distilled skeleton but the warmth of the original words. After a full production audit (2🔴5🟡), every finding is fixed and shipped with this release: the B4 injection frame is now enforced at the server-side recall exit (production is self-defending with or without the hook), call_llm is hardened against the gateway's fake-SSE responses and auto-retries with a bigger budget on reasoning-model truncation so the governance evaluator is alive again, noise rules catch keyboard-mash junk, backup_gate is wired into the upgrade flow as a hard gate, ledger target_id queries expand aliases so one parameter retrieves the whole history, and secondary write paths (federation/refine/ai-self) get governance and ledger coverage. Full suite at the time of v19.4.0: 244 passed. (Latest figures: see "Testing & Quality" below.) v19.3.3 · Athena — Architectural Unification & Audit-Driven Hardening. Building on v19.2.0 production hardening, the v19.3 series delivers **single-source-of-truth unification of the recall pipeline and scoring engine**: funnel stages fully delegate to the unified 5-dimension scoring, singleton and lazy-import lifecycle hardened with double-checked locking, the 800+ line legacy module decoupled, and a unified injection-defense gate placed before final persistence. v19.3.1–v19.3.3 are consecutive audit-driven fixes: silent-exception observability, reranker placeholder removal, legacy route import completion (fixing /facts/add 500), nested exception-handler regression fix, and test assertion alignment.

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

## Pantheon of Gods

> Each major version of aiduMEI is named after a Greek deity — the god's domain reflects the architecture.

| Version | Codename | Deity | Core Mission |
|---------|----------|-------|-------------|
| **v19.4.2** | **Athena** | Goddess of Wisdom · Guard Coverage & Credential Wiring | **Meta-test welds the guard's range · 8 credential entry points on one source of truth · `.env` fallback chain through standalone integrations · crashloops become visible · rotation stops losing logs · wordmark residue cleared · configuration written ≠ configuration in effect (`StartLimit*` section) · reproducible both ways is what makes it falsifiable (`HERMES_SRC` three-state)** |
| **v19.4.1** | **Athena** | Goddess of Wisdom · Audit Patch | **One gate, two keys · tenant visibility scoping & cross-tenant overwrite fix · cascade delete covers the Verbatim Vault · idempotency key & Chinese index alignment** |
| **v19.4.0** | **Athena** | Goddess of Wisdom · Project Mirror · Audit Fix | **Verbatim Vault · Verbatim-evidence fused recall · Server-side injection frame · LLM hardening · Noise rule upgrade · Backup hard gate · Ledger alias expansion · Secondary-path governance & ledger** |
| **v19.3.3** | **Athena** | Goddess of Wisdom · Architectural Unification | **Single-source scoring · Singleton concurrency hardening · Unified injection gate · Silent-exception observability · Legacy decoupling** |
| **v19.2.0** | **Athena** | Goddess of Wisdom · Production Hardening | **Prompt injection defense · Multi-store cascade delete & WAL · Unified scoring · Dynamic health** |
| **v19.0** | **Athena** | Goddess of Wisdom · From Memory to Wisdom | **Active Reflect · memory self-editing · recursive refinement · skill growth · persona memory layer** |
| **v18.3** | **Zeus** | King of the Gods · Multimodal | Lossless fast-update · multimodal vision memory · Obsidian bi-directional links · console password change |
| **v18.2** | **Zeus** | King of the Gods · Insight | Built-in aiduMEI console · EvolveMem feedback loop · quality audit |
| **v18.1** | **Zeus** | King of the Gods · Self-Evolving | EvolveMem feedback loop · 38 MCP tools · quality audit |
| **v18.0** | **Zeus** | King of the Gods · Power Absorption | Raw Drawer · Code Graph · 5 competitors精华 fusion · MCP×36 · IDE hooks |
| **v17.0** | **Themis** | Goddess of Order | Event ledger · sensitivity tiers · governance rules |
| **v16.0** | **Opus Octopod** | Deep-sea Sage | Conflict resolution · tree memory · skill crystallization |
| **v15.0** | **Iris** | Rainbow Messenger | Official MemoryProvider channel · lazy hot-reload |
| **v14.0** | **Aegis** | Divine Shield | Zero hardcoding · privacy shield · deploy anywhere |
| **v13.0** | **Pantheon** | Hall of Gods | Multi-agent federation · MoE gating |
| **v12.0** | **Chronos** | God of Time | Dual timeline validity |
| **v11.0** | **Hyperion** | Titan of Light | Thread-local connection pool · performance era |
| **v9.1** | **Mnemosyne** | Goddess of Memory | Tidal coalescing · dual-strategy tiering |

[Full version history →](CHANGELOG.md)

---

## 🛡️ v19.2.0 Production Hardening Highlights

> Verified in real-world production environments (1,000+ active production facts) and comprehensive security audits, v19.2.0 delivers 6 key production-grade hardenings:

1. **3-Layer Prompt Injection Defense & Context Sandboxing** (`ducky/security/injection_guard.py`)
   - **Multi-layer Filter**: Layer 1 regex pattern filter (jailbreaks / prompt overrides), Layer 2 normalization filter (strips punctuations/spaces to defeat obfuscation bypasses), and Layer 3 repetition/overflow rate-limiter.
   - **Benign Whitelist**: Whitelists legitimate DevOps phrases and common natural language patterns to prevent false positives.
   - **Context Sandboxing**: Recalled memories injected into System Prompts are strictly wrapped with `[DATA: MEMORY CONTEXT ...]` boundary tags, declaring them as pure data.
2. **Tenant Ownership Checks & Exact-Match Deletion (P0)** (`ducky/hot/crud.py` & `ducky/wal_engine.py`)
   - **Strict Tenant Scoping**: `/delete` and `/update` enforce tenant ownership (`user_id`), eliminating cross-tenant access.
   - **Exact Matching**: Replaced substring SQL `LIKE '%...%'` queries with exact `id=? OR fact_key=?` matching, preventing accidental substring deletions.
   - **Scope, not a security boundary (please do not over-read)**: aiduMEI is a **single-machine self-hosted** engine. The tenant dimension separates different agents/identities inside one deployment; it is **not** equivalent to the security boundary of a multi-tenant SaaS. Recall-side scoping covers the facts layer as of v19.4.1 (`AIDUMEM_STRICT_TENANT` switches to strict mode — see [`.env.example`](.env.example)). To host mutually untrusted parties, isolate by deployment instance rather than relying on this layer.
3. **Anti-Accidental Clear Guard (P0)**
   - `/delete_all` strictly rejects empty payloads with HTTP 400.
   - Purging the `default` tenant requires explicit `confirm: true` to prevent accidental wipeouts.
4. **Multi-Store Cascade Atomic Deletion & Application WAL (P0)** (`ducky/wal_engine.py`)
   - Single deletion and tenant wipeouts synchronously purge **Qdrant vector store, SQLite FTS5 full-text index, facts.db, salience.db, evolve_mem.db**, and — as of v19.4.1 — the **Verbatim Vault** (`verbatim_turns` + `verbatim_fts_map`), leaving zero orphan records.
   - Lightweight `wal_journal.jsonl` with `fsync` logging; automatically reconciles and self-heals orphaned records via `reconcile_startup()` on boot.
   - Recursive refinement soft-archives vector points and unindexes FTS5 records, eliminating ghost memory recalls.
5. **Unified 5-Dimension Scoring Engine & Zero N+1 Queries (P1)** (`ducky/scoring.py`)
   - Standardized scoring across Vector + BM25 + Time Decay + Reliability + Heat with a single truth decay constant `AIDUMEM_RECENCY_LAMBDA`.
   - **Zero N+1 Query Overhead**: `get_batch_memory_types` loads 6-type classifications via a single SQL batch query.
6. **Network / Credential Hardening & Live Degradation Telemetry (P1)** (`ducky/degradation.py` & `api_server.py`)
   - Binding to public interfaces (`0.0.0.0`) without `AIDUMEM_API_TOKEN` raises a fatal error on boot to prevent unprotected exposure.
   - Eliminates default weak passwords; automatically generates a strong random password, persisted as a hash in `data/.ui_password_hash` (as of v19.4.1: PBKDF2-HMAC-SHA256 with 200k rounds, file mode 0600, legacy hashes auto-upgraded on first successful login).
   - **Unified auth gate (v19.4.1)**: after console login the server issues an HttpOnly session cookie, which together with `Authorization: Bearer <AIDUMEM_API_TOKEN>` forms one gate with two keys — either grants access. Previously the console password was a frontend-only marker with no effect on REST endpoints. See `probes.auth_gate_enabled` in `/health`.
   - `/health` exposes live `degraded_components` and memory high-watermark capacity warnings (>800 facts).

---

## 🦉 What's New in v19.0 · Athena — From Memory to Wisdom

> Zeus solved *what to remember, how to store it, how to retrieve it*. Athena closes the second half of the cognitive loop: **once a memory is stored, how does the Agent actively review, self-correct, refine over time, grow experience into skills, and hold a stable persona?** Memory no longer only grows — it reflects, converges, and evolves.

- **🔮 Active Reflect (P0-3 · inspired by Hindsight)** — Periodic/triggered review distills patterns, relations, predictions, contradictions and knowledge gaps into first-class `reflections`. Auto-runs every 6h (configurable) and on `/session/end`; degradation-friendly. New: `POST /reflect`, `GET /reflect/list`, `GET /reflect/context`.
- **✏️ Memory Self-Editing (P0-2 · inspired by Mem0)** — Before writing, an LLM judges *duplicate / conflict / novel* against existing memory — merge instead of append, keep both on conflict with confidence. Jaccard fallback when LLM is down; every edit snapshotted to `memory_edits`, one-click rollback.
- **🧬 Recursive Refinement (P1-3 · inspired by SimpleMem)** — Background clustering compresses many fragment memories into higher-level abstractions. Products land in `refined_memories`; originals soft-superseded (never physically deleted), fully reversible.
- **🌱 Autonomous Skill Growth + Pruning (P1-2 · inspired by ReMe/MemU)** — Task trace → step extraction → LLM-drafted SKILL.md → **human approval** → archived skill. Reuse scoring; low-utility skills (success rate < 34%) auto-flagged for retirement without deletion. LLM can only draft, never auto-commit.
- **🎭 Persona Memory Layer (inspired by MemoryForge)** — Expands a one-line persona into a full context-retrievable autobiographical memory bank (L/G/E three tiers), replacing a static persona card injected every turn. Dual modes: `synthesis` (fictional characters) / `grounded` (real users, from existing memory, no fabrication). Versioned & reversible.
- **🕰️ Dual-Timeline Memory + Time-Aware Retrieval (P0-1 / P0-4)** — Every memory carries `valid_from` / `valid_to` / `recorded_at`; hybrid retrieval fuses vector + BM25 + time decay + reliability + heat, with a tunable decay rate.
- **🗂️ Memory Type Separation (P1-1 · inspired by Hindsight's four networks)** — Six explicit cognitive types: FACTS · PREFERENCES · EXPERIENCES · OBSERVATIONS · REFLECTIONS · DECISIONS.

---

## 📦 Deployment Footprint — Light Enough for an Entry-Level VPS

> A common question: how heavy is this to run? **Answer: very light — by design.**

| Dimension | Measured | Notes |
|-----------|----------|-------|
| **Memory** | **~210 MB RSS** (single process, measured) | mem0 kernel + FastAPI + embedded vector store in one Python process |
| **CPU** | **2 cores plenty, < 1% idle** | No heavy resident compute; LLM/Embedding all via external API |
| **Disk (program)** | ~2.6 MB source · ~175 MB venv | Pure Python, no compilation, clone & run |
| **Disk (data)** | ~13 MB vectors + few-hundred KB SQLite per thousands of memories | Grows linearly, tiny scale |
| **Direct deps** | **only 9 top-level packages** | mem0ai / qdrant-client / fastapi / uvicorn / pydantic family / httpx / requests |
| **Python** | 3.10 – 3.12 | 3.12 recommended |
| **Frontend** | **0 dependencies** | Pure static console — no node, no bundler, no compile |

**Why it's this light, deliberately:**

- **Embedded on-disk vector store** (Qdrant `path` mode) — no separate service, no Docker, no extra port.
- **Compute outsourced** — LLM / Embedding / Rerank all via OpenAI-compatible APIs; no model weights loaded locally, so no GPU, no big RAM.
- **Relevance gate first** — casual chat skips retrieval, saving tokens and compute.
- **SQLite + FTS5 fallback** — structured knowledge and full-text search on zero-dependency SQLite; hot-switches from vector to full-text on timeout.

> In short: **a 1-core/1 GB entry VPS runs it; 2-core/2 GB is comfortable.** The heavy lifting (LLM inference) lives in the cloud API — locally it's a lean memory-and-retrieval brain.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│        🦉 aiduMEI v19.4.2 · Athena · AI Wisdom Engine │
│              FastAPI REST API :8767                       │
│              MCP Server :8768 (41 tools)                  │
├──────────────────────────────────────────────────────────┤
│  Athena          → Reflect · Self-Edit · Refine · Skill Growth · Persona │
│  Core (HOT)      → Search, Add, CRUD, Health              │
│  v8 Pipeline     → Ignition · Workspace · Broadcast ·     │
│                    Mirror · Session                        │
│  Clotho/Hyperion → CoreMemory · Checkpoint · AutoDream    │
│  Extended        → Auto-memory · Expiry · Stats           │
│  Federation      → Multi-agent Fed · MoE gate · 4-tier    │
│  Octopus         → Conflict · Tree Memory · Crystals      │
│  Zeus            → Raw Drawer · Code Graph · Evolve       │
│  Themis          → Event Ledger · Sensitivity · Audit     │
├──────────────────────────────────────────────────────────┤
│  mem0 (vector memory) + Qdrant (embedding store)          │
│  facts.db (structured knowledge · FTS5 trigram search)    │
│  EvolveMem self-evolving retrieval engine                 │
└──────────────────────────────────────────────────────────┘
```

---

## Quick Start

### Method 1: Run via Docker (GitHub Packages / GHCR)

```bash
docker pull ghcr.io/monkey2jack/aidumei:latest
docker run -d -p 8767:8767 --name aidumei ghcr.io/monkey2jack/aidumei:latest
```

### Method 2: Clone & Run from Source

```bash
# 1. Clone
git clone https://github.com/monkey2jack/aiduMEI.git
cd aiduMEI

# 2. Create virtual environment
python3.12 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure (copy and edit)
cp mem0_config_local.json.example mem0_config_local.json
# Edit mem0_config_local.json with your LLM and embedding API keys

# 5. Start
python api_server.py
# API runs on http://localhost:8767
```

---

## Core API Endpoints

### Memory Operations

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/search` | Search memories (hybrid: vector + BM25 + relevance gate) |
| `POST` | `/search_trace` | Search with full execution trace |
| `POST` | `/add` | Add memories (async tidal coalescing by default) |
| `POST` | `/add/raw` | Raw Drawer — zero-LLM verbatim storage |
| `DELETE` | `/delete` | Delete a memory by ID |
| `GET` | `/health` | Health check with full probe diagnostics |

### Code Graph (Zeus v18.0)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/code/impact` | Analyze file change blast radius |
| `GET` | `/code/graph` | View full project dependency graph |

### Retrieval Evolution (Zeus v18.1)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/evolve/feedback` | Submit retrieval quality feedback (useful / useless / correction) |
| `GET` | `/evolve/report` | Evolution stats panel (recall rate, weight adjustment history) |

### Octopus Governance (Opus v16.0)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/conflict/resolve` | Conflict resolution (domain migration, name changes auto-detect) |
| `GET` | `/tree/nodes` | Tree memory node listing |
| `POST` | `/crystals/detect` | Detect crystallizable high-frequency facts |
| `GET` | `/crystals` | View skill crystal candidates |

### Athena Cognitive Layer (v19.0)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/reflect` | Trigger active reflection into insights |
| `GET` | `/reflect/list` | List stored reflection insights |
| `GET` | `/reflect/context` | Injectable reflection summary |
| `GET` | `/self-edit/edits` | Memory self-edit (merge/conflict) history |
| `POST` | `/self-edit/rollback` | Roll back a self-edit |
| `GET` | `/memory/types` | Six memory types & distribution |
| `POST` | `/memory/types/query` | Retrieve memories by type |
| `POST` | `/memory/refine` | Trigger recursive refinement |
| `POST` | `/memory/refine/rollback` | Roll back a refinement |
| `POST` | `/skill/grow` | Grow a SKILL.md draft from a task trace (needs approval) |
| `POST` | `/crystals/use` | Skill reuse scoring (success/fail) |
| `POST` | `/crystals/prune` | Retire low-utility skills (archive, not delete) |

### Persona Memory Layer (v19.0)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/persona/build` | Build a persona bank (`synthesis` / `grounded` dual mode) |
| `GET` | `/persona/banks` | List persona banks |
| `POST` | `/persona/retrieve` | Context-based persona retrieval |
| `POST` | `/persona/rollback` | Roll back to a historical persona version |

### Pantheon Federation (v13.0)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/federation/recall` | Federated recall (MoE gate auto-decides hot/fed channel) |
| `POST` | `/federation/facts/add` | Federated write (auto dedup + tiering + attribution) |
| `GET` | `/federation/agents` | Agent list with fact counts & online status |
| `POST` | `/federation/agents/register` | Register an agent to the federation |
| `GET` | `/federation/broadcast` | Pull new shared facts from other agents |
| `GET` | `/federation/awareness` | Federation situational summary |

### Examples

```bash
# Search memories
curl -s -X POST http://localhost:8767/search \
  -H "Content-Type: application/json" \
  -d '{"query": "What was the project deadline I mentioned?", "user_id": "me", "limit": 5}'

# Add a memory
curl -s -X POST http://localhost:8767/add \
  -H "Content-Type: application/json" \
  -d '{"messages": "[{\"role\":\"user\",\"content\":\"Project deadline is March 15\"}]", "user_id": "me"}'

# Raw Drawer — store code snippets verbatim, zero LLM
curl -s -X POST http://localhost:8767/add/raw \
  -H "Content-Type: application/json" \
  -d '{"content": "def hello(): print(\"Hello World\")", "source": "my_script.py", "user_id": "me"}'

# Blast radius analysis
curl -s -X POST http://localhost:8767/code/impact \
  -H "Content-Type: application/json" \
  -d '{"file_path": "ducky/utils.py"}'

# Retrieval feedback — tell the system how good the search was
curl -s -X POST http://localhost:8767/evolve/feedback \
  -H "Content-Type: application/json" \
  -d '{"query": "project deadline", "rating": "useful", "user_id": "me"}'
```

---

## What Makes aiduMEI Unique

### 🔮 Relevance Gate (Tahoe-Gate)
Most RAG systems search memory for every single message. aiduMEI's **Relevance Gate** (`GET /gate`) uses heuristics + dynamic entity matching to determine if the current message actually needs memory retrieval. Casual chat skips retrieval entirely → saves tokens and compute. Hosts call the gate before injecting memory context.

### 🌊 Tidal Coalescing (Mnemosyne Tidal)
Short messages don't trigger individual LLM calls. They're buffered asynchronously by session, then batched into a single LLM call. Three-tier strategy: Tech / Intimate / Default — fast for code, deep for personal.

### ⏳ Three-Lane Ebbinghaus Decay
Memories have expiration dates. Identity and Preference are permanent lanes (zero decay), Emotion decays 1.5× faster, general facts follow the standard forgetting curve. **Teach AI to forget what doesn't matter.**

### 🕰️ Chronos Dual Timeline
`valid_from` / `valid_to` time windows: expired facts are deprioritized but never deleted, future facts are sorted behind. All governance-type memories (identity/preference lane) never expire.

### ⚡ Raw Drawer (Zeus v18.0)
Inspired by MemPalace's (58k⭐) Verbatim Storage. Zero-LLM raw text storage — code snippets, full conversations, raw logs bypass LLM summarization entirely. FTS5 full-text index + Qdrant vector + facts registration, three pipelines in parallel.

### 🔍 Code Graph (Zeus v18.0)
Inspired by code-review-graph's (29k⭐) AST blast radius analysis. Uses Python's standard `ast` library to parse project dependencies. Change one file, instantly see the impact. 724 functions · 936 imports, full-graph scan in 468ms.

### 📈 EvolveMem Self-Evolving Retrieval (Zeus v18.1)
Inspired by SimpleMem's (3.7k⭐) evolution concept. Users rate each retrieval result (useful / useless / correction). Background thread runs every 6 hours to auto-compute decay/boost. High-quality frequent entries auto-consolidate, low-quality ones gently deprioritize. **Closed-loop feedback — gets smarter with use.**

### 🏛️ Pantheon Federation
Inspired by MoE (Mixture-of-Experts): a complete multi-agent federation infrastructure underneath, with only the current agent's hot channel active day-to-day.

- **Federated Identity**: Every memory carries `agent_id` / `profile` / `shared` — multiple agents share one database without cross-contamination
- **MoE Gating**: Default hot channel (single SQL, 5ms level); other agents only awakened on explicit request
- **Four-Tier Graceful Degradation**: L1 local → L2 tiered-weight → L3 same-profile federation → L4 cross-profile global
- **Write Dedup**: Jaccard three-state — ≥0.85 merge, ≥0.70 update, <0.70 insert

### 🐙 Conflict Resolution & Skill Crystallization (Opus Octopod — v16.0)

- **ConflictResolver**: Domain migrations, name changes auto-detected + old values deprioritized. Dual timeline invalidation instead of deletion
- **TreeMemory**: `node_path` hierarchical tracing, facts mounted to tree nodes, ancestor traversal supported
- **SkillCrystallizer**: Background auto-detection of high-frequency repeated facts,提炼ed into Skill candidates. **LLM can only suggest — human approval required to activate**

### 🛡️ Aegis Shield (v14.0)
Zero hardcoded identities, absolute paths, server addresses, or secrets in the repository. Everything configurable goes through environment variables. Clone to any directory, any machine — `python api_server.py` just works.

### 🌈 Iris Rainbow Bridge (v15.0)
aiduMEI provides an **official Hermes Agent MemoryProvider plugin** with full lifecycle hooks — turn-start injection of persistent blocks & relevant retrieval, background archiving every turn, **pre-compress rescue of about-to-be-discarded conversations into long-term memory**, mirroring of the host's built-in MEMORY.md writes, and three directly callable tools.

```bash
cp -r integrations/hermes-plugin/aidumei ~/.hermes/plugins/
hermes config set memory.provider aidumei
```

### 🔧 Zero-Config Hybrid Search
BM25 trigram (zero-latency fallback) + vector embedding vectors + Reranker + recall funnel relevance ranking. Vector service timeout triggers automatic hot-switch to local full-text search.

---

## Hermes Agent Integration

| Method | Capabilities | When to Use |
|--------|-------------|-------------|
| **A. MemoryProvider Plugin** (recommended) | Full lifecycle hooks + tools + backup | Default choice |
| **B. Shell Hook** | Turn-start injection only | When host can't install plugins |

**Do not enable both simultaneously** (duplicate injection wastes tokens). See [integrations/INTEGRATION_GUIDE.md](integrations/INTEGRATION_GUIDE.md) for full steps, verification, and rollback.

> ⚠️ **Security**: aiduMEI does not implement authentication itself and listens on `127.0.0.1` by default. For remote access, place a reverse proxy with authentication + TLS in front. Never expose the service directly to the public internet.

---

## MCP Server (41 Tools)

aiduMEI includes a built-in MCP Server (`:8768`) exposing 41 tools:

| Tool Group | Count | Description |
|------------|-------|-------------|
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
| Persona (v19.0) | 3 | persona_build / persona_retrieve / persona_banks |

---

## IDE Integration

### Cursor

```bash
# Copy rule file to project
cp integrations/cursor-hook/cursor-aidumei.mdc .cursor/rules/

# Auto-store on file save → Raw Drawer
cp integrations/cursor-hook/aidumei-on-save.sh .git/hooks/post-commit
```

### Claude Code

```bash
python integrations/cursor-hook/claude-code-hook.py store --file my_code.py
python integrations/cursor-hook/claude-code-hook.py search --query "database connection"
python integrations/cursor-hook/claude-code-hook.py impact --file ducky/utils.py
```

---

## Tech Stack

- **Runtime**: Python 3.12+, FastAPI, Uvicorn
- **Memory Kernel**: mem0 v2.0.18
- **Vector Store**: Qdrant (via qdrant-client)
- **Structured Data**: SQLite (facts.db, observations.db, scenes.db, fact_events.db)
- **Full-Text Search**: SQLite FTS5 + trigram tokenizer
- **Embeddings**: Configurable (OpenAI Embedding API compatible)
- **Reranking**: Configurable (OpenAI Rerank API compatible)
- **LLM**: Any OpenAI-compatible API
- **MCP**: fastmcp stdio + HTTP dual-mode

---

## Testing & Quality

```bash
# Full regression suite
pytest tests/
# Compile check
python -m compileall ducky api_server.py mcp_server.py
```

**Honest reporting of test scope (v19.4.1)**

| Dimension | Status |
|-----------|--------|
| Total cases | **403** (measured via `pytest --collect-only`) |
| Clean dev machine | 391 passed · **12 skipped** — the skipped ones require the host Hermes source tree, unavailable in a bare checkout |
| Complete environment | **403 all green** (with the Hermes source present, all 12 run and pass; verified on production) |
| Layers | Mostly module-level unit tests + source-level guard assertions; `TestClient`-driven API tests as a secondary layer |
| Statement coverage | ~51% (`ducky/` plus entrypoints, measured with `coverage`) |
| Not covered | Real mem0/Qdrant integration, real LLM calls, concurrency stress — these depend on external services and are covered by production smoke tests |

> **Why report both 391 and 403**: the same suite yields different numbers in different environments,
> and quoting only one of them misleads the reader. The 12-case gap is exactly the set of integration
> tests that need the host Hermes source: without it pytest reports `skipped` (not failed); with it they all pass.
> Always state the environment alongside a test count.
>
> **Those 12 are reproducible, not folklore** (added in v19.4.2): they all live in
> `tests/test_hermes_plugin.py` and skip when the host's `agent/memory_provider.py` cannot be found.
> `HERMES_SRC` is a three-state switch, so **both directions reproduce**:
>
> ```bash
> pytest tests/ -q -rs | tail -1                                 # no host: 391 passed, 12 skipped
> HERMES_SRC=/path/to/hermes-agent pytest tests/ -q | tail -1    # with host: 403 passed
> HERMES_SRC=none pytest tests/ -q -rs | tail -1                 # host present but forced off: 391 passed, 12 skipped
> ```
>
> A "skip" you cannot turn back into a "pass" is just an unfalsifiable number — **and the converse holds
> too**. On a machine that happens to have the host installed (`/hermes/hermes-agent` is auto-discovered;
> our own production box is exactly that), the first command above actually prints 403 passed, 0 skipped.
> Without the `HERMES_SRC=none` state, a reader simply cannot reproduce the "12 skipped" we claim.
> **Falsifiability requires reproducibility in both directions.**
>
> Also: if `HERMES_SRC` points somewhere without `agent/memory_provider.py`, resolution **raises**
> instead of silently falling back to an auto-discovered path — pointing at A while testing B,
> under a green light, is the hardest kind of false green to catch.

**Why spell this out**: v19.4.0's README only said "full suite: 244 passed", which reads like end-to-end assurance.
But 244 cases finishing in 0.88s clearly involve no real external dependency. More importantly, v19.4.0's
idempotency test was green while only covering `list[dict]` payloads carrying explicit timestamps — production
actually sends plain strings without timestamps, and a real bug shipped through that gap under a green light.

Since v19.4.1 we enforce an **anti-false-green rule**: any test touching payload shape, credential shape, or
query shape must cover *every* shape; performance and index assertions must verify self-evident fields such as
`_recall_path` rather than merely checking "did we get a hit".

---

## Security Model (v19.4.1)

**One gate, two keys.** Both are accepted; either one grants access:

| Key | Who uses it | How |
|-----|-------------|-----|
| Session cookie | Browser console | `POST /login` with the console password; the server issues an HttpOnly, SameSite=Lax session cookie |
| Bearer token | Scripts, MCP, CI | `Authorization: Bearer <AIDUMEM_API_TOKEN>` |

The gate activates when **either** `AIDUMEM_API_TOKEN` is set **or** the console password is set explicitly
(via env var, or by changing it through the console). A password auto-generated at first boot guards the console
login only — it deliberately does **not** activate the REST gate, so existing loopback callers (Hermes plugin,
MCP, cron) keep working across an upgrade. Check `probes.auth_gate_enabled` in `/health` to see the current state.

**Tenant scoping is not a SaaS security boundary.** aiduMEI is a single-machine self-hosted engine; the tenant
dimension separates different agents/identities within one deployment. Recall-side scoping covers the facts layer
as of v19.4.1, and `AIDUMEM_STRICT_TENANT=1` switches to strict mode (no fallback for unlabeled historical rows).
If you need to host mutually untrusted parties, isolate by deployment instance rather than relying on this layer.

**Passwords** are stored as PBKDF2-HMAC-SHA256 (200k rounds) in `data/.ui_password_hash` with mode 0600;
pre-v19.4.1 single-round SHA-256 hashes are upgraded automatically on first successful login.

---

## Configuration

aiduMEI reads configuration from `mem0_config_local.json`. Key fields:

```json
{
  "llm": {
    "provider": "openai",
    "config": {
      "model": "your-model",
      "api_key": "your-key",
      "base_url": "your-endpoint"
    }
  },
  "embedder": {
    "provider": "openai",
    "config": {
      "model": "your-embedding-model",
      "api_key": "your-key",
      "base_url": "your-embedding-endpoint"
    }
  },
  "vector_store": {
    "provider": "qdrant",
    "config": {
      "collection_name": "aidu_mem",
      "host": "localhost",
      "port": 6333
    }
  }
}
```

---

## Environment Variables

Since v14 Aegis, all deployment-specific settings are injected via environment variables — **all optional**, safe defaults when unset.

| Variable | Default | Description |
|----------|---------|-------------|
| `AIDUMEM_HOME` | Repo root (auto-detected) | Override repository root |
| `AIDUMEM_DATA_DIR` | `<repo>/data` | Database & vector store location |
| `AIDUMEM_LOG_DIR` | `<repo>/logs` | Log directory |
| `AIDUMEM_CONFIG_FILE` | `<repo>/mem0_config_local.json` | mem0 config file path |
| `AIDUMEM_DEFAULT_USER_ID` | `default` | Default user_id |
| `AIDUMEM_DEFAULT_AGENT_ID` | `default` | Federation default agent_id |
| `AIDUMEM_ENTITY_KEYWORDS` | empty | Custom entity keywords for relevance gate, `\|` separated |
| `AIDUMEM_URL` | `http://127.0.0.1:8767` | Hermes plugin / hook service URL |
| `AIDUMEM_USER_ID` | `default` | Hermes plugin / hook memory namespace |
| `AIDUMEM_MIN_HISTORY` | `6` | shell hook: skip injection when session history below this |

Full list with comments: [`.env.example`](.env.example). Start with `cp .env.example .env`.

---

<p align="center">
  <sub>AI Wisdom Engine · Athena | Built by <a href="https://github.com/monkey2jack">aiduMEI Team</a></sub>
</p>