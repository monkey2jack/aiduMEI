# 🤔 aiduMEM — Hermes AI Thought Engine

> **Not just memory — thinking.**
>
> *Optimization is not refactoring code, but implanting excellent logic;*
> *Memory is not记事, but never forgetting the details of the past;*
> *Thinking is not reasoning, but doing everything with reason and result.*

[![Version](https://img.shields.io/badge/version-15.0.0%20·%20Iris-blue.svg)](https://github.com/monkey2jack/aiduMEM)
[![PyPI](https://img.shields.io/pypi/v/aidumem.svg)](https://pypi.org/project/aidumem/)
[![Docker Image](https://img.shields.io/badge/docker-ghcr.io-blue?logo=docker)](https://github.com/monkey2jack/aiduMEM/pkgs/container/aidumem)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-yellow.svg)](https://www.python.org/)
[![Built on mem0](https://img.shields.io/badge/built%20on-mem0-orange.svg)](https://github.com/mem0ai/mem0)

**[📖 中文文档](README.md)** | **English**

---

## What is aiduMEM?

aiduMEM is an **AI Thought Engine** — a persistent memory and reasoning system for Hermes AI agents. It goes far beyond simple key-value storage: aiduMEM **remembers, thinks, and evolves**.

Built on top of [mem0](https://github.com/mem0ai/mem0), aiduMEM adds a complete **cognitive architecture** on top:

| Layer | What it does | Key feature |
|-------|-------------|-------------|
| 🧠 **Recall** | Find the right memory at the right time | Ebbinghaus decay + BM25/trigram + vector hybrid search |
| 🔍 **Gate** | Only retrieve what's actually relevant | Relevance Gate blocks irrelevant context — 100x faster |
| 🌊 **Tidal** | Batch LLM extraction, not one-by-one | Coalescing queue: async short messages → single LLM call |
| 📊 **Evolution** | Knowledge grows and self-corrects | Trust scoring, user correction awareness, consolidation |
| ⏳ **Decay** | Forgetting is a feature, not a bug | Emotion lane accelerated decay, identity lane zero-decay |
| 🕰️ **Chronos** | Time-aware validity | Dual timeline (valid_from / valid_to), expired facts deprioritized |
| 🏛️ **Pantheon** | Many agents, one memory | Federated identity + MoE gating + 4-tier graceful degradation |
| 🛡️ **Aegis** | Zero hardcoding, clone and run | Identity / paths / keyword lists all injected via env vars |
| 🌈 **Iris** | Rides the host's native memory channel | Hermes MemoryProvider plugin: pre-compress rescue · memory mirroring · direct tools |

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│        🤔 aiduMEM — Hermes AI Thought Engine     │
│              FastAPI REST API :8767               │
├──────────────────────────────────────────────────┤
│  hot/        → Search, Add, CRUD (main path)     │
│  speed/      → Async coalescing + fastpath        │
│  pipeline/   → Recall funnel + hybrid search      │
│  salience/   → Trust scoring + conflict detection │
│  legacy/     → Facts, observations, scenes        │
│  extended/   → Auto-memory, workspace, broadcast  │
│  federation/ → Multi-agent federation · MoE gate  │
├──────────────────────────────────────────────────┤
│  mem0 (vector memory) + Qdrant (embedding store)  │
│  facts.db  (structured knowledge)                 │
│  FTS5 trigram (full-text search fallback)         │
└──────────────────────────────────────────────────┘
```

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

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/search` | Search memories (hybrid: vector + BM25 + relevance gate) |
| `POST` | `/add` | Add memories (async coalescing by default) |
| `DELETE` | `/delete` | Delete a memory by ID |
| `GET` | `/health` | Health check with probe diagnostics |
| `POST` | `/search_trace` | Search with full execution trace |
| `POST` | `/graduate` | Scan and graduate low-value memories |
| `POST` | `/scene/cluster` | Cluster observations into scenes |
| `GET` | `/usage` | Token usage statistics |
| `GET` | `/add/coalesce/stats` | Tidal coalescing statistics |
| `GET` | `/federation/recall` | Federated recall (MoE gate picks hot vs federated channel) |
| `POST` | `/federation/facts/add` | Federated write (auto-dedup + tiering + ownership) |
| `GET` | `/federation/agents` | List agents with fact counts and liveness |
| `POST` | `/federation/agents/register` | Register an agent into the federation |
| `GET` | `/federation/broadcast` | Pull newly shared facts from peer agents |
| `GET` | `/federation/awareness` | Federation situational summary |
| `GET` | `/federation/tiers` | Tier distribution and decay configuration |

### Example: Search

```bash
curl -s -X POST http://localhost:8767/search \
  -H "Content-Type: application/json" \
  -d '{"query": "What did I say about the project deadline?", "user_id": "me", "limit": 5}'
```

### Example: Add Memory

```bash
curl -s -X POST http://localhost:8767/add \
  -H "Content-Type: application/json" \
  -d '{"messages": "[{\"role\":\"user\",\"content\":\"Project deadline is March 15th\"}]", "user_id": "me"}'
```

## What Makes aiduMEM Different

### 🔮 Relevance Gate
Normal RAG systems search memories for every message. aiduMEM's **Relevance Gate** uses heuristics + dynamic entity matching to determine if the current message actually needs memory retrieval. Casual chat is skipped → **100x token savings**, response time drops from 10ms to 1ms.

### 🌊 Tidal Coalescing
Short messages aren't processed one-by-one. Async buffer groups by session, one LLM call handles multiple messages. Tech/intimate/default three-tier strategy.

### ⏳ Ebbinghaus Decay
Memories have expiry dates. Identity and Preference lanes are permanent (zero-decay), Emotion is accelerated decay (1.5x multiplier), general facts fade naturally. **Teach AI to forget unimportant things.**

### 🕰️ Chronos Dual Timeline
`valid_from` / `valid_to` time windows: expired facts are deprioritized but not deleted, future facts are ranked lower. All "iron law" memories (identity/preference lane) never expire.

### 🏛️ Pantheon Federation
> All the gods live in the Pantheon — but you only summon the one you need.

Borrowed from MoE (Mixture-of-Experts): **build the full multi-agent federation underneath, activate only the current agent's hot channel day-to-day.**

- **Federated identity**: every fact carries `agent_id` / `profile` / `shared`, so multiple agents share one store without polluting each other
- **MoE gating**: defaults to the hot channel (single SQL, ~5ms); peers are only summoned on explicit request or when the query carries federation intent. Single-agent setups never pay the federation cost
- **4-tier graceful degradation**: L1 own agent → L2 tier-weighted → L3 same-profile federation → L4 cross-profile global. Any failing tier falls through to the next — the chain never dies
- **Tiered decay**: `episodic` 30 days, `semantic` 180 days, `procedural` **never decays**. Decay only deprioritizes; nothing is deleted
- **Write-time dedup**: Jaccard three-way verdict — ≥0.85 merge, ≥0.70 update, <0.70 insert. Not writing garbage is a hundred times cheaper than cleaning it later
- **Cursor-based broadcast**: pull peers' newly shared facts with no duplicates and no gaps; read-only aggregation, no replicas

```bash
# Register an agent into the federation
curl -X POST "http://localhost:8767/federation/agents/register?agent_id=agent_b&profile=default"

# Federated recall (gate decides the channel)
curl "http://localhost:8767/federation/recall?query=project+deadline&agent_id=agent_b&top_k=5"
```

### 🔧 Zero-Config Hybrid Search
BM25 trigram (zero-latency fallback) + BGE-M3 vectors + Reranker reranking + recall funnel relevance ranking. Vector service timeout auto-switches to local full-text search.

### 🌈 Iris Rainbow Bridge (v15, new)
> A messenger's job isn't just delivering — it's confirming the message arrived.

v15 ships a native **Hermes Agent MemoryProvider plugin**, so memory injection no
longer depends on a shell script parsing payload fields:

```bash
cp -r integrations/hermes-plugin/aidumem ~/.hermes/plugins/
hermes config set memory.provider aidumem
```

That wires up the full lifecycle: turn-start injection of the resident CoreMemory
block plus per-turn recall, background archiving of every turn, **rescuing
about-to-be-dropped turns into long-term memory right before compaction**,
mirroring of the host's built-in MEMORY.md writes, three callable tools
(`aidumem_search` / `aidumem_remember` / `aidumem_status`), and the data directory
registered with the host's backup flow. See
[integrations/INTEGRATION_GUIDE.md](integrations/INTEGRATION_GUIDE.md).

The same release kills three classes of **silent failure** — a broken injection
chain, a missing keyword list, and missing startup config all used to fail without
a word. Now each one speaks up:

- shell hook payload parsing accepts three shapes (`extra.conversation_history` / top-level / legacy `messages`)
- the relevance gate and entity extractor compile keyword lists **lazily with hot reload**, instead of freezing them at import time
- when `AIDUMEM_ENTITY_KEYWORDS` is unset, both the startup log and the `/health` probe say so explicitly

## Hooking into Hermes Agent

| Method | Capability | When |
|--------|-----------|------|
| **A. MemoryProvider plugin** (recommended) | Full lifecycle hooks + tools + backup | Default choice |
| **B. Shell hook** | Turn-start injection only | When you can't install plugins on the host |

**Don't enable both** — you'd inject twice and burn tokens for nothing. Full setup,
verification and rollback steps live in
[integrations/INTEGRATION_GUIDE.md](integrations/INTEGRATION_GUIDE.md).

> ⚠️ **Security**: the aiduMEM service has no built-in authentication and listens on
> `127.0.0.1` by default. For cross-machine access, put an authenticating TLS reverse
> proxy in front — exposing the service directly to the internet makes every memory
> publicly readable and writable.

## Tech Stack

- **Runtime**: Python 3.12+, FastAPI, Uvicorn
- **Memory Core**: mem0 v2.0.5
- **Vector Store**: Qdrant (via qdrant-client)
- **Structured Data**: SQLite (facts.db, observations.db, scenes.db)
- **Full-Text Search**: SQLite FTS5 with trigram tokenizer
- **Embedding**: Configurable (OpenAI-compatible Embedding API)
- **Reranker**: Configurable (OpenAI-compatible Rerank API)
- **LLM**: Configurable via any OpenAI-compatible API

## Configuration

aiduMEM reads configuration from `mem0_config_local.json`. Key sections:

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

## Environment Variables

All deployment-specific settings are injected via env vars and **every one is
optional** — unset means a safe default. Annotated full list in
[`.env.example`](.env.example); start with `cp .env.example .env`.
A systemd unit template is at [`deploy/aidumem-api.service`](deploy/aidumem-api.service).

| Variable | Default | Purpose |
|----------|---------|---------|
| `AIDUMEM_DATA_DIR` | `<repo>/data` | Where databases and vectors land |
| `AIDUMEM_ENTITY_KEYWORDS` | empty | Custom entity list for the relevance gate, `\|`-separated (e.g. `Alice\|Bob\|ProjectX`). **Leave this unset and queries about your own names/projects silently return nothing.** |
| `AIDUMEM_URL` | `http://127.0.0.1:8767` | Service address used by the Hermes plugin / hook |
| `AIDUMEM_USER_ID` | `default` | Memory namespace for the plugin / hook |
| `AIDUMEM_MIN_HISTORY` | `6` | Shell hook: skip injection below this many history messages |

## Roadmap

- [ ] MCP (Model Context Protocol) server mode
- [x] Multi-agent / multi-profile federated memory (v13.0 Pantheon ✅)
- [x] Zero-hardcode portable deployment (v14.0 Aegis ✅)
- [x] Native Hermes MemoryProvider plugin (v15.0 Iris ✅)
- [ ] Cross-machine federation (HTTP peer pull, not shared DB)
- [ ] Multi-user workspace isolation
- [ ] Memory consolidation dashboard
- [ ] Plugin system for custom decay curves
- [ ] REST API → GraphQL adapter

## Contributing

Contributions welcome! Please open an issue first to discuss what you'd like to change.

## License

MIT License — see [LICENSE](LICENSE) for details.

---

<p align="center">
  <sub>Thinking Version · Iris | Built by <a href="https://github.com/monkey2jack">monkey2jack</a> & <a href="https://github.com/monkey2jack">dudu</a></sub>
</p>
