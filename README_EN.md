<p align="center">
  <img src="assets/aidumei-banner.webp" alt="aiduMEI ⚕ 爱嘟优忆思 — Memory + Engine + Insight" width="100%">
</p>

<!-- distribution-policy: github-source-only -->

# aiduMEI ⚕ YouiSi — a general-purpose wisdom engine for agents

> Make your AI agent **actually remember you**: hybrid retrieval + cognitive governance + a visual console + a dual-engine autoshift, a **single-machine self-hosted** engine, MIT.
> Your host (Hermes / Claude Code / Cursor / any MCP client) owns the short-term conversation; aiduMEI owns long-term memory.

> The current public release is **f0.1**.
>
> **About the `f`**: this is a new era, not a continuation of the old numbering. `f` stands for
> **future / fantasy / forever** — what we want to build is not a bigger cache, but a memory that
> can walk with someone for a long time. The version shape is `f<major>.<minor>`: routine iterations
> bump the minor, only a disruptive rework bumps the major. Every earlier version lives in
> [CHANGELOG](CHANGELOG.md); this README answers exactly one question: **what it is right now.**
>
> **Status labels**: rapid feature iteration · external calibration 1/4 (✅ benchmark trial run done, mistakes published · ⬜ dependencies · ⬜ third-party reproduction · ⬜ independent review).
> Checking out a tag ≠ a releasable build; `pyproject.toml` is the source of truth.

---

## YouiSi: MEI is more than "beauty"

**MEI = Memory + Engine + Insight.** In Chinese it reads 优忆思 (YouiSi) — three characters, three
commitments, and **each one maps to code that actually runs in this repo**, not to marketing copy:

| | What it means | What it is in the code |
|---|---|---|
| **You (优)** · Engine | **Tuned configuration, dual engines, fully automatic** | Dual-engine autoshift (downshifts to a local spare the moment the cloud fails, upshifts on recovery) · one-line Prompt deployment · read/write/distill hooks fire by themselves — once wired, you never manage memory again |
| **i (忆)** · Memory | **The memory substrate and its logic** | Three-track forgetting curves · dual timeline (memories **expire** rather than get deleted) · event time stored separately from ingest time · vectors + Chinese BM25/trigram + a real cross-encoder reranker (not weighted fusion) · six-type classification |
| **Si (思)** · Insight | **Borrow the model's reasoning; spend less of your context** | A relevance gate drops small talk before retrieval · reranking narrows the set before it reaches the context · session distillation compresses a whole run into one entry · reflection and self-evolution (`reflect.py` / `evolve_mem.py`); cognitive governance backstops it — the AI may only *propose* candidate knowledge and **has no authority to create facts** |

The second half of that middle row is this era's theme. The system used to answer "what did you say"
but stumbled on "when was that" — because on write, the **time the thing happened** was silently
replaced by the **time the record landed**, with no error and no warning: retrieval kept returning
results and the health check stayed green. This release fixes it in all three places: write,
retrieval, and injection (see [CHANGELOG](CHANGELOG.md)).

## Three things nobody else ships

> ⚠️ **Evidence status**: the three below are a **unique feature combination** (no isomorphic
> implementation on the market). "First of its kind" refers to the combination, not to a measured
> ranking. For numbers, see the next section — we publish the mistakes too.

| Killer feature | One line |
|---|---|
| 🚗 **Dual-engine autoshift** | Cloud outage auto-downshifts to a local spare, recovery auto-upshifts, backlog auto-replays — legs swap mid-query, and the current gear is always honestly visible |
| 📊 **Visual console** | Zero-build web console (`/ui`): memory visible, tunable, traceable; switch between real memory domains and export the current one to Markdown — we answer the "memory is a black box" complaint head-on |
| 🧠 **Cognitive governance** | Every memory has an **origin** (you said it / AI inferred it / external citation / unknown, tagged at zero write cost), a **paper trail** (who, which session, which turn), and is **exportable** (one-click Markdown dossier) |

## 📊 Benchmarks: we ran them, and we publish the mistakes

**First LoCoMo benchmark trial run completed 2026-09-22.** Not one number in the table below is derived.

> ⚠️ **The scale matters, or this table will mislead you.** Memory benchmarks use **two rulers that
> must never be mixed**: **F1** (token overlap — different wording loses points, strict) and
> **LLM-Judge** (semantic equivalence, lenient). The "LoCoMo 90%+" figures in industry marketing are
> almost all Judge-scale and method-iterated. **This table is F1 throughout**; industry numbers come
> from the Mem0 paper ([arXiv:2504.19413](https://arxiv.org/abs/2504.19413) Table 1, each system self-reported).

| Dimension (F1) | LangMem | Zep | OpenAI full-context | Mem0 | **aiduMEI trial** | Our standing |
|---|---|---|---|---|---|---|
| Single-hop recall | 35.51 | 35.74 | 34.30 | **38.72** | **37.37** | 🥈 2nd |
| Multi-hop reasoning | 26.04 | 19.37 | 20.09 | **28.64** | 23.19 | Mid-pack (beats Zep / OpenAI) |
| Temporal reasoning | 30.75 | **42.00** | 14.04 | **48.93** | 25.79 | ❌ Clear weakness |
| Open-domain knowledge | 40.91 | 49.56 | 39.31 | 47.65 | 9.87 | ⚠️ Only 13 questions — **not statistically meaningful** |
| Adversarial abstention | — | — | — | — | **81.69** | ⭐ Usually absent from the industry's four-way comparisons |

**Trial conditions** (reproduction anchors): official LoCoMo dataset `3eb6f2c`, **first 2 complete samples**
(conv-26 / conv-30) · 788 turns ingested + 304 questions · embeddings BAAI/bge-m3 ·
answering model Claude-Sonnet-4.6 · hybrid retrieval top_k=5 · **zero-correction baseline** ·
overall F1 42.14% / Judge 52.96%.

**How to read this table**:

- ✅ **The two claims that hold up**: **single-hop factual recall reaches the top tier** (2nd overall,
  ahead of Zep / OpenAI full-context / LangMem); **hallucination resistance leads by a wide margin**
  (adversarial abstention F1 81.69) — faced with trap questions about things never mentioned in the
  conversation, the gating philosophy rarely lets it be talked into inventing an answer.
- ❌ **What we must admit**: **temporal reasoning is a real weakness** (25.79 vs Zep 42 / Mem0 48.93);
  those 13 open-domain questions **do not support any conclusion**.
- ⚠️ **What we will not claim**: this is a **trial run**, not a final score. 2 of 10 samples, and the
  judge was Sonnet rather than the industry-standard GPT-4o. **We do not and will not claim SOTA on this basis.**

### This release's targets (**not yet re-measured**)

The most glaring weakness in the table above — temporal reasoning — **is something this release
already worked on**: the root cause was traced to "event time silently replaced by ingest time on
write" and fixed in all three places at once: write, retrieval, and injection (see [CHANGELOG](CHANGELOG.md)).

But **this release did not re-run the evaluation**, so not a single entry in the "target" column is a result yet:

| Dimension | Trial (measured) | Target | What this release did | Status |
|---|---|---|---|---|
| **Temporal reasoning** | 25.79 | **≥ 40** (vs Zep) | **Main focus of this release**: event time recorded correctly · passed through `/search` · rendered with the date on injection | 🔧 **Fixed, re-run pending** |
| Single-hop | 37.37 | Hold ≥ 37 | Retrieval main path untouched; should not regress | 🛡️ Hold |
| Adversarial abstention | 81.69 | Hold ≥ 80 | Gating philosophy unchanged | 🛡️ Hold |
| Multi-hop reasoning | 23.19 | ≥ 28 (vs Mem0) | Untouched this release | ⬜ Deferred (iterative retrieval / query expansion) |
| Open-domain | 9.87 | Full re-run for a real value | Untouched this release | ⬜ Deferred (widen the sample first) |

> **Why "fixed" still isn't a score**: writing the right code does not mean the number moves.
> That has to be settled by a full re-run. Until then, the right-hand side of the table states
> **what we intend to deliver**, not what we have already delivered.

> **Why publish an unflattering score first**: the value of a first benchmark run is exposing problems,
> not collecting numbers. This trial immediately surfaced a root cause that had been hiding for a long
> time (event time silently replaced by ingest time) — far more useful than a pretty figure. A formal
> leaderboard entry requires all 10 samples re-run with a GPT-4o judge, and we will **publish the
> failures alongside the wins**. The evaluation protocol is frozen under `benchmarks/` (dataset, models,
> judge, prompt, seed and file hashes all pinned and recorded).

## One-prompt deploy: let your Agent do the work, you watch

Send this to your AI agent:

> You are now a deployment engineer. Follow the full `prompts/install.txt` (the 14-line canon) from <https://github.com/monkey2jack/aiduMEI> to deploy aiduMEI on this machine. Verify every step yourself; never pretend success.

The canon walks it through: environment check → dependencies → gear selection → keys (it asks you, never invents them) → start the service → **real write/recall verification** (not just `/health`) → host integration → cron jobs and backups → a report for you.

> ⚠️ **Wire two lines to the host, not one.** The **read line** injects memory *before* each turn — if it breaks you notice immediately. The **write line** stores the turn *afterwards*, and **if that breaks you may not notice for weeks**: retrieval still returns results and `/health` still shows green, because the old memories really are healthy — while every new thing you say is being dropped.
>
> We paid for this lesson in production (read line down for a month, write line never wired, every probe green). So now `/health` carries an `ingest_liveness_ok` probe watching for "reading but not writing", and after a few real turns you should run:
>
> ```bash
> python3 scripts/check_ingest_wiring.py --token "$AIDUMEM_API_TOKEN"   # exit code 0 means the wiring works
> ```
>
> **Each line has a ready-made script — copy it over and register it** (don't write your own):
>
> | | Script | Hook point | What it does automatically |
> |---|---|---|---|
> | Read line | `integrations/aidumem-inject.sh` | Hermes `pre_llm_call` | Feeds relevant memories to the model **before** each turn |
> | Write line | `integrations/aidumem-ingest.sh` | Hermes `post_llm_call` | Stores the turn **after** it happens |
> | Write line | `integrations/cursor-hook/claude-code-stop-hook.py` | Claude Code `Stop` | Same |
> | Distill line | `integrations/aidumem-distill.sh` | Hermes `on_session_end` | At session end, distills "the thing most worth remembering from this run" into its own entry |
>
> **"Automatic" means that once these three are wired, you never have to do anything about memory** — no manual saving, no reminding the model to remember, no periodic tidying. The three hooks fire on their own before you speak, after you speak, and when the session ends. Your only job is to hook them in the right places and verify once with the command above.
>
> The distill line solves a different kind of forgetting: per-turn writes store **facts**, but cannot store "what this run was about" — an offhand remark, a problem solved together, the moment a decision was made, all scatter into a dozen facts and never resurface. It runs in its own slow-decay lane (`distill`, kept longer than ordinary memories), and its emotional weighting comes from the repo's existing sentiment lexicon, not a newly invented score.
>
> All three ship with `--selftest` (the write line really writes an entry and reads it back). But **a passing self-test only proves the script runs, not that the host is calling it** — in that incident the script was fine all along, it simply was not hooked up. That is why `check_ingest_wiring.py` is the only acceptance criterion. Full wiring instructions and YAML in [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md).

### ⚠️ Upgrading? You must redeploy the hooks

**Hooks are copies, not symlinks.** A `git pull` updates `integrations/*.sh` in the repo, but the host still runs the **old files** — no error, no warning, clean logs, `/health` all green. The read line silently reverts to its old behaviour while you believe you have upgraded.

There is a nastier second layer: **the filename the host actually calls may differ from the repo's.** An early install can leave an alias behind. Verifying by filename checks a file the host never executes — and hands you a false "already deployed".

**So after every upgrade (and any time you want to confirm "is the host running this version?"):**

```bash
python3 scripts/check_hook_deployment.py     # exit code 0 = actually deployed
```

It **ignores filenames and trusts only `~/.hermes/config.yaml`**: whatever path the host declares is the path whose md5 gets compared. On drift it prints the exact fix command. With no hooks installed it reports "not measured" rather than "pass".

This check is folded into `scripts/health_check.py`, so **your scheduled health check already covers it — no extra cron entry needed**.

> One line to remember: **verify against the file the host actually calls — verifying the repo file is not verifying at all.**
> This is what a user audit caught us on (fix written, tests green, report sent — just never delivered to the host).

**No agent? Five lines by hand:**

```bash
git clone https://github.com/monkey2jack/aiduMEI.git && cd aiduMEI
python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp mem0_config_local.json.example mem0_config_local.json && cp .env.example .env   # edit both, add your keys
python api_server.py                                                              # → http://127.0.0.1:8767
python scripts/e2e_smoke.py --json                                                # real verification; only PASS counts
```

**Three gears — pick by your machine** (`AIDUMEI_ENGINE_MODE=auto` is the recommended default; `cloud` / `local` likewise):

| Gear | Requires | Effect |
|---|---|---|
| `cloud` | Cloud LLM + embedding keys | Lightest; on outage, recall degrades honestly |
| `auto` (recommended) | Keys + `pip install .[local-embed]` | Cloud first, auto-switch to local on outage, auto-switch back on recovery |
| `local` | Local models, zero keys | Zero tokens, zero outbound calls |

Containerised deployment: [docs/DEPLOY_DOCKHOLD.md](docs/DEPLOY_DOCKHOLD.md). The full agent-side operating manual (acceptance probes, backups, maintenance) is in [AGENTS.md](AGENTS.md).

## 📦 Load & consumption — both sizes, measured and on the table

> Is this heavy to run? **Depends on the gear.** (2-core 3.5 GB cloud VM · measured 2026-08-27)

| Dimension | ☁️ Cloud (`cloud`) | ⚙️ Autoshift (`auto`, default) | 🔋 Local (`local`) |
|------|---------------------|--------------------------|---------------------|
| **Resident memory** | **~280 MB** | **~430 MB** | ~430 MB |
| **Dependency disk** | ~275 MB | ~353 MB + 91 MB model | Same as autoshift |
| **On outage** | No spare; honestly reports `degraded` | **Auto-downshifts and keeps going** | No external dependency |
| **Token usage** | Normal | Normal (zero during outage) | **Always zero** |
| **Keys required** | Yes | Yes (without them it stays local) | **No** |

**Shared**: 2 CPU cores is enough, <1% idle; `/search` 0.14–0.23 s; cold start 5.2 s; a thousand-memory data dir is ~13 MB of vectors plus a few hundred KB of SQLite; zero frontend dependencies; Python 3.10–3.12.

**Where those 150 MB go, and whether they can be cut** (measured, not estimated): the onnxruntime library alone, imported without loading a model, is **75 MB**; the bge-small-zh-v1.5 session and weights are **~122 MB**; the measured delta between the two gears is **150 MB** (components measured separately and sharing pages with the gear delta — both figures stated honestly, not summed). We tried to shrink it: `threads=1`, on-demand ONNX arena allocation, `malloc_trim`, `MALLOC_ARENA_MAX=2` — **all four knobs measurably did nothing**; the model is already the smallest usable Chinese option in the fastembed catalogue (the next smallest multilingual option is 2.4× its size). So we did not fake an optimisation — we gave you a switch instead: **if you don't need the spare, pick the cloud gear and those 150 MB cost you nothing.**

> **Why the spare stays resident**: dual indexing requires every write to compute a local vector at the same time — loading the model at the moment of an outage would recall nothing. The spare is prepared in advance, not found on demand.
> The rest of the lightness is deliberate: the vector store is embedded on disk (no separate process or port), no GPU, the relevance gate drops small talk first, SQLite+FTS5 as a fallback. In short: **the cloud gear runs on 1 core / 1 GB; autoshift and local want 2 cores / 2 GB.**

> **Pick a gear by available memory (deploying agents: follow this)**: plenty of headroom (≥2 GB free) and you want "cloud is down but local can still recall everything" → `auto` (the local spare stays **hot-resident**; those ~174 MB buy you that offline resilience). Tight memory, or no need for an offline fallback → `cloud` (~280 MB; if the cloud dies it honestly reports `degraded` and does not fall back locally). **Don't let a memory-starved machine default to `auto` and then complain it's heavy** — gears exist so you can choose by headroom; run `free -m` before you pick.
>
> **Planned cold-spare gear**: the local model is **not resident** (saving those ~174 MB in normal operation) and loads only when the cloud fails, then batch-computes local vectors for existing memories; measured on a small-to-medium store (~1500 entries) that catch-up window is on the order of ten-odd seconds, `degraded` throughout, full capability once complete. It balances "save memory normally" against "full resilience after a failure", at the cost of a catch-up window after failure (growing linearly with store size; large stores need incremental catch-up). This addresses the "autoshift should load on demand" critique directly — the hot-spare (current `auto`) versus cold-spare trade-off will be folded into the gear-selection guide.

## The console: memory is not a black box

Start the service and open `http://127.0.0.1:8767/ui`: browse/search/adjust memories, health probes and gear status, federation and evolution, a retrieval quality panel, and **one-click dossier export** (Markdown, partitioned by origin, with every inference explicitly marked "unverified"). The domain selector at the top only lists `(user_id, bank_id)` pairs that are registered and active server-side; an invalid domain falls back to the server default, and when the directory is unavailable it does not guess a demo domain. A federation member's `profile` is a display grouping, not a memory domain.

## Integrations

| Host | Method |
|---|---|
| Hermes Agent | MemoryProvider plugin: auto-save and auto-recall every turn (pre-compression rescue) |
| Claude Code | CLI hook / MCP |
| Cursor | Rules file (auto-saves to Raw Drawer on file save) |
| Any MCP client | MCP Server (41 tools, default :8766, stdio/HTTP dual transport) |
| Anything else | REST API (:8767) |

Details: [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md).

## MCP Server (41 tools · default port 8766)

MCP and REST run in one process: REST on :8767, MCP on :8766 (stdio/HTTP dual transport). **Auth discipline**: a non-loopback bind must configure `AIDUMEM_API_TOKEN` or the server refuses to start; only set `AIDUMEM_ALLOW_INSECURE_PUBLIC=1` if you genuinely need public exposure (off by default; turning it on emits a critical log line). Tool groups and call examples in [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md).

## Security Model

Bearer token (`AIDUMEM_API_TOKEN`) + console passphrase (PBKDF2) + injection defences + loopback-only by default. After startup, check Three probes first: `health_status`, `degraded`, and `probes.runtime_paths.data_dir_writable` on `/health` (probes are redacted without credentials, with a `_redacted` note). Multiple rounds of external security audit are logged line by line in [docs/SECURITY-AUDIT-LEDGER.md](docs/SECURITY-AUDIT-LEDGER.md).

## Key environment variables

| Variable | Purpose | Default |
|---|---|---|
| `AIDUMEM_API_TOKEN` | API auth token (mandatory for non-loopback) | empty = loopback only |
| `AIDUMEM_ENTITY_KEYWORDS` | Entity lexicon (names/project codenames, fed to the relevance gate) | empty |
| `AIDUMEM_DATA_DIR` | Data directory | `~/.aidumem` |
| `AIDUMEI_ENGINE_MODE` | Engine gear: cloud/auto/local | auto |
| `AIDUMEM_CONFIG_READONLY` | Read-only demo mode for console config | 0 |
| `AIDUMEI_INJECT_DATE` | Timestamp in recalled items: `day`/`minute`/`off` (hook side) | day |

The complete environment variable registry is `ducky/env_registry.py` (code is the source of truth; typos raise a startup warning).

> **Dual prefixes are frozen**: new variables are always `AIDUMEI_` (`AIDUMEM_` is legacy, no new additions).
> Existing variables are not migrated (compatibility red line) — documentation only.

## Capability map (one table, no stories)

| Layer | Capability |
|---|---|
| Retrieval | bge-m3 vectors + FTS5 Chinese BM25/trigram + a real cross-encoder reranker; relevance gate (small talk skips retrieval, saving tokens) |
| Memory semantics | Three-track forgetting (identity never decays / emotional accelerates / standard curve) · dual timeline (memories **expire** rather than get deleted) · six-type classification |
| Governance | Dual review on write + conflict resolution + injection defence; event ledger across all paths; cryptographic lineage (tamper-evident) |
| Evolution | Reflection (on-demand and scheduled) · instinct-to-skill promotion (human approval gate) · retrieval self-evolution feedback loop |
| Collaboration | Federation: multiple agents share one memory store (MoE gating + fine-grained grants) · multiple bots/profiles each in their own domain, independent memory personas, cross-domain isolation by default |
| Periphery | Multimodal visual memory · code graph · verbatim drawer · Obsidian backlinks |

## Testing & quality

**Test levels, stated honestly**
> How to read the table: "passed · skipped" per row sums to the `pytest --collect-only` count **for that shape, on that date**; different rows may have different denominators (the tree grows), so trust the date in each row. Skips are explained per "axis" (see [docs/TESTING.md](docs/TESTING.md)) and are not failures.

| Dimension | Current |
|------|------|
| Total cases | **2237** (measured via `pytest --collect-only`, 2026-09-23, f0.1 tree) = **2031 behavior (product code under direct test) + 70 script/hook + 136 guard (docs/consistency/structure)**. Split methodology and the file lists live in `scripts/count_test_kinds.py` and can be recomputed in one command — no blended number in the headline |
| Clean dev machine | 2225 passed · **12 skipped** — **collected 2026-09-23** (f0.1 tree, Python 3.12; complete extras and model cache, only Hermes source absent) |
| Basic install path | 1821 passed · **25 skipped** — requirements files only, clean Python 3.12 venv (**measured 2026-09-09 on the production box**) |
| Sandbox on the production box | 1967 passed · **26 skipped** — **measured 2026-09-11** (this tree de09794, separate sandbox venv on the production box: host source present, no `.env`, optional axes absent); production host post-deploy: 1983 passed · 10 skipped (same tree, host axes present) |
| All axes present | 1844 passed · **1 skipped** — **measured 2026-09-09 on the production host** (isolated full-axis venv: tools, extras, host source, model cache and the public LoCoMo dataset all present; that single skip is a conditional axis on a newly added case) |
| Levels | Primarily **module-level unit tests plus source-level guard assertions**, with `TestClient`-driven interface tests as support |
| Platform | The full suite is maintained for **Linux/macOS (POSIX)**: the `backup_gate` axis needs a POSIX shell; `/health` CPU/RSS metrics go through the `resource` module and honestly report `None` on non-POSIX platforms rather than crashing. Windows is not a full-suite platform |
| Statement coverage | ~51% (`ducky/` + entry points, measured with `coverage`) |
| Not covered | Real mem0 / Qdrant integration, real LLM calls, concurrency stress — these depend on external services and are carried by production smoke tests |

```bash
# Full regression
pytest tests/
# Compile check
python -m compileall ducky api_server.py mcp_server.py
```

> **Why report both 2225 and 1821**: the first is the 2026-09-23 collection count of the complete optional environment on this tree; the second is the 2026-09-09 clean-venv measurement of the basic install path (requirements files only). A number only means anything with its environment and date attached.

> **Those 12 skips are not hand-waving — you can verify them yourself**: all thirteen skip axes (host, tooling, optional dependencies, model files) are registered in [docs/TESTING.md](docs/TESTING.md); `HERMES_SRC` is tri-state and reproducible in both directions:
>
> ```bash
> # measured 2026-09-14: install everything, deploy the model cache, and point AIDUMEI_BENCH_DATA_DIR at a directory containing locomo10.json
> pip install -r requirements.txt -r requirements-dev.txt
> pip install "mcp>=1.0.0,<2" ruff nltk regex numpy fastembed
> python scripts/fetch_local_embed_model.py
> pytest tests/ -q -rs | tail -1                                 # no host: 2225 passed, 12 skipped
> HERMES_SRC=/path/to/hermes-agent pytest tests/ -q | tail -1    # with host: 2237 passed
> HERMES_SRC=none pytest tests/ -q -rs | tail -1                 # forced off: 2225 passed, 12 skipped
> ```
>
> `2237 passed` in the block above requires **all thirteen axes present**; the host is only one of them — don't read "install the host" as "all green".
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
> | `mcp` extra installed | 8 | MCP import-surface guards + auth-behavior + SSE transport cases + search session passthrough |
>
> On the production box in an isolated sandbox (host source present, no `.env`), the bare command actually prints 1967 passed, 26 skipped (measured 2026-09-11, tree `de09794`) — axes differ, so numbers only travel with their environment and date.

## Security & compliance

MIT License. `SECURITY.md` + [docs/SECURITY-AUDIT-LEDGER.md](docs/SECURITY-AUDIT-LEDGER.md): multiple rounds of external security audit logged line by line (including our reasons for rejecting false positives). The MCP layer forces a token on non-loopback binds or refuses to start.

## Known boundaries (honestly stated)

- Requires embedding and LLM services (cloud or a local spare) — what you get back is real semantic retrieval and extraction quality. If you want "fully offline and sub-millisecond", a zero-dependency local tool suits you better, and we won't hide that.
- **The benchmark result is a trial run, not a final score**: 2 of 10 samples, judge is not GPT-4o. A formal entry requires a full re-run; [benchmarks/RESULTS.md](benchmarks/RESULTS.md) records this honestly.
- Temporal reasoning remains a **known weakness**: the root cause is fixed but **not yet re-measured** — every entry in the "target" column above is still a target, not a result.
- Measured recall overlap between the local lite gear and the cloud gear is limited (every difference is priced openly in docs) — that is exactly why the auto gear exists.

## Docs

Deployment and operations: [🤖 Agent Guide](AGENTS.md) (the single entry point for agents) · [docs/HEALTH.md](docs/HEALTH.md) · [docs/OPERATIONS.md](docs/OPERATIONS.md) · [TROUBLESHOOTING.md](TROUBLESHOOTING.md) · [docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md) · [docs/POSITIONING.md](docs/POSITIONING.md) (peer comparison, every figure recomputable) · [docs/BENCHMARKING-POSTURE.md](docs/BENCHMARKING-POSTURE.md) (benchmark posture and scales)

## Known Limitations & Not Covered

| # | Exception | Notes |
|---|------|------|
| 1 | Tenant isolation narrows visibility per tenant | It is not a strong isolation layer for mutually distrusting customers; the domain contract is `ducky/bank_contract.py`, with boundaries recorded in `docs/SECURITY-AUDIT-LEDGER.md`. |
| 2 | `fetch_local_embed_model.py` must run at deploy time | Zero network at runtime; `ducky/local_embed.py` forces `HF_HUB_OFFLINE=1`. After fetching, every file is sha256-verified against `scripts/local_embed_model_sha256.json`; a mismatch deletes the file and exits non-zero. |
| 3 | `capture_wave` recalls nothing if `entity_keywords` is unset | No error is raised; configure `AIDUMEM_ENTITY_KEYWORDS`. See `ducky/pipeline/memory_gate.py`. |

## Repository layout

```text
aiduMEI/
├── AGENTS.md / llms.txt    # Agent deployment entry point and doc index
├── api_server.py           # Main entry (API + /ui console hosting)
├── ducky/                  # Business logic (hot/ pipeline/ speed/ salience/ federation/ evolve_mem.py …)
├── frontend/               # Console (zero-build static; js/vendor/ holds echarts locally)
├── benchmarks/             # Evaluation protocol (dataset/models/judge/seed/hashes all pinned)
├── tests/                  # Regression suite (pytest)
├── prompts/install.txt     # One-line Prompt deployment canon
├── docs/                   # Operations/health/backup/capacity/testing methodology
├── scripts/                # e2e_smoke.py · check_hook_deployment.py · report.py and friends
└── mem0_config_local.json  # Model configuration (gitignored, holds keys)
```

## License

MIT — see [LICENSE](LICENSE).

<p align="center">
  <sub>aiduMEI⚕YouiSi (formerly aiduMEM / duMem, preserved in historical versions and docs)｜Powered by monkey²</sub>
</p>
