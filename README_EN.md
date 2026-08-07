<p align="center">
  <img src="assets/aidumem-banner.jpg" alt="aiduMEM" width="100%">
</p>

# 🤔 aiduMEM — AI Thought Engine

> **Not just memory — thinking.**
>
> *Optimization is not refactoring code, but implanting excellent logic;*
> *Memory is not note-taking, but never forgetting the details of the past;*
> *Thinking is not reasoning, but doing everything with reason and result.*

[![Version](https://img.shields.io/badge/version-18.1.0%20·%20Zeus-blue.svg)](https://github.com/monkey2jack/aiduMEM)
[![PyPI](https://img.shields.io/pypi/v/aidumem.svg)](https://pypi.org/project/aidumem/)
[![Docker Image](https://img.shields.io/badge/docker-ghcr.io-blue?logo=docker)](https://github.com/monkey2jack/aiduMEM/pkgs/container/aidumem)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-yellow.svg)](https://www.python.org/)
[![Built on mem0](https://img.shields.io/badge/built%20on-mem0-orange.svg)](https://github.com/mem0ai/mem0)

**[📖 中文文档](README.md)** | **English**

---

## What is aiduMEM?

aiduMEM is an **AI Thought Engine** — a persistent memory and reasoning system for AI Agents. Named after the Greek gods, it embodies a complete **cognitive architecture** that enables AI to **remember, think, and evolve**.

Built on top of [mem0](https://github.com/mem0ai/mem0), aiduMEM adds a ten-layer cognitive framework:

| Layer | Codename | What it does | Key Feature |
|-------|----------|-------------|-------------|
| 🧠 **Recall** | Mnemosyne | Find the right memory at the right time | Ebbinghaus decay + BM25/trigram + vector hybrid search |
| 🔍 **Gate** | Tahoe-Gate | Only retrieve what's actually relevant | 1ms heuristic gate blocks irrelevant context — 100× token savings |
| 🌊 **Tidal** | Mnemosyne Tidal | Batch LLM extraction, not one-by-one | Async coalescing queue: multiple short messages → single LLM call |
| ⏳ **Decay** | Ebbinghaus | Forgetting is a feature, not a bug | Three-lane decay: Identity zero-decay / Emotion accelerated / General standard curve |
| 🕰️ **Chronos** | Chronos | Time-aware validity | Dual timeline (valid_from / valid_to), deprioritize without deletion |
| 🏛️ **Pantheon** | Pantheon | Many agents, one memory | Federated identity + MoE gating + 4-tier graceful degradation |
| 🛡️ **Aegis** | Aegis | Zero hardcoding, clone and run | Identity / paths / keywords all injected via env vars |
| 🌈 **Iris** | Iris | Rides the host's native memory channel | Hermes MemoryProvider plugin: pre-compress rescue · memory mirroring · direct tools |
| 🐙 **Octopod** | Opus Octopod | Memory governance & crystallization | ConflictResolver + TreeMemory + SkillCrystallizer |
| ⚡ **Zeus** | Zeus | King of the Gods | Raw Drawer + Code Graph + EvolveMem self-evolving retrieval |

---

## Pantheon of Gods

> Each major version of aiduMEM is named after a Greek deity — the god's domain reflects the architecture.

| Version | Codename | Deity | Core Mission |
|---------|----------|-------|-------------|
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

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│           🤔 aiduMEM v18.1 · Zeus · AI Thought Engine     │
│              FastAPI REST API :8767                       │
│              MCP Server :8768 (38 tools)                  │
├──────────────────────────────────────────────────────────┤
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

### Method 1: Install via PyPI

```bash
pip install aidumem
```

### Method 2: Run via Docker (GitHub Packages / GHCR)

```bash
docker pull ghcr.io/monkey2jack/aidumem:latest
docker run -d -p 8767:8767 --name aidumem ghcr.io/monkey2jack/aidumem:latest
```

### Method 3: Clone & Run from Source

```bash
# 1. Clone
git clone https://github.com/monkey2jack/aiduMEM.git
cd aiduMEM

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

## What Makes aiduMEM Unique

### 🔮 Relevance Gate (Tahoe-Gate)
Most RAG systems search memory for every single message. aiduMEM's **Relevance Gate** uses heuristics + dynamic entity matching to determine if the current message actually needs memory retrieval. Casual chat skips entirely → **100× token savings**, response latency drops from 10ms to 1ms.

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
aiduMEM provides an **official Hermes Agent MemoryProvider plugin** with full lifecycle hooks — turn-start injection of persistent blocks & relevant retrieval, background archiving every turn, **pre-compress rescue of about-to-be-discarded conversations into long-term memory**, mirroring of the host's built-in MEMORY.md writes, and three directly callable tools.

```bash
cp -r integrations/hermes-plugin/aidumem ~/.hermes/plugins/
hermes config set memory.provider aidumem
```

### 🔧 Zero-Config Hybrid Search
BM25 trigram (zero-latency fallback) + BGE-M3 vectors + Reranker + recall funnel relevance ranking. Vector service timeout triggers automatic hot-switch to local full-text search.

---

## Hermes Agent Integration

| Method | Capabilities | When to Use |
|--------|-------------|-------------|
| **A. MemoryProvider Plugin** (recommended) | Full lifecycle hooks + tools + backup | Default choice |
| **B. Shell Hook** | Turn-start injection only | When host can't install plugins |

**Do not enable both simultaneously** (duplicate injection wastes tokens). See [integrations/INTEGRATION_GUIDE.md](integrations/INTEGRATION_GUIDE.md) for full steps, verification, and rollback.

> ⚠️ **Security**: aiduMEM does not implement authentication itself and listens on `127.0.0.1` by default. For remote access, place a reverse proxy with authentication + TLS in front. Never expose the service directly to the public internet.

---

## MCP Server (38 Tools)

aiduMEM includes a built-in MCP Server (`:8768`) exposing 38 tools:

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

---

## IDE Integration

### Cursor

```bash
# Copy rule file to project
cp integrations/cursor-hook/cursor-aidumem.mdc .cursor/rules/

# Auto-store on file save → Raw Drawer
cp integrations/cursor-hook/aidumem-on-save.sh .git/hooks/post-commit
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
- **Memory Kernel**: mem0 v2.0.17
- **Vector Store**: Qdrant (via qdrant-client)
- **Structured Data**: SQLite (facts.db, observations.db, scenes.db, fact_events.db)
- **Full-Text Search**: SQLite FTS5 + trigram tokenizer
- **Embeddings**: Configurable (OpenAI Embedding API compatible)
- **Reranking**: Configurable (OpenAI Rerank API compatible)
- **LLM**: Any OpenAI-compatible API
- **MCP**: fastmcp stdio + HTTP dual-mode

---

## Configuration

aiduMEM reads configuration from `mem0_config_local.json`. Key fields:

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
      "model": "BAAI/bge-m3",
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

## Roadmap

- [x] **v18.1 Zeus** — EvolveMem self-evolving retrieval feedback loop
- [x] **v18.0 Zeus** — Raw Drawer · Code Graph · MCP×36 · IDE hooks
- [x] **v17.0 Themis** — Event ledger · sensitivity tiers · governance rules
- [x] **v16.0 Opus Octopod** — Conflict resolution · tree memory · skill crystallization
- [ ] **v19.0** — Multimodal memory (image → visual description → storage)
- [ ] **v20.0** — OpenViking unified context DB fusion

---

<p align="center">
  <sub>Thinking Version · Iris | Built by <a href="https://github.com/monkey2jack">monkey2jack</a> & <a href="https://github.com/monkey2jack">dudu</a></sub>
</p>