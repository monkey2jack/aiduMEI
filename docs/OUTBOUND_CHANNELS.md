# Outbound Channels — What Leaves the Host, and When

> **Audience**: deployers deciding whether and how aiduMEI may talk to the
> network. Every egress path in the codebase is listed here with the exact
> fields it sends. Nothing else leaves the host: there is no telemetry, no
> update check, no crash reporting, no license phone-home.

## The one gate

`ducky/engine_mode.cloud_egress_allowed(which)` is checked **before every
outbound request**. With `AIDUMEI_ENGINE_MODE=local` it returns `False` for all
channels below; callers degrade locally (return `None` / empty) instead of
raising. With `auto` (default) or `cloud`, the gate is open and each channel
fires only when its own trigger runs. Blocked calls are counted and surfaced at
`/health` (`engine_mode` probe, `cloud_egress_blocked_counts`).

## Channels

| # | Channel | Fires when | Endpoint | Fields sent | Gated |
|---|---|---|---|---|---|
| 1 | LLM distillation (`ducky/llm_client.py::call_llm`) | `/add` with `infer=true`; reflect / governance / persona / distill / self-edit and 9 modules that call `call_llm` | `llm.openai_base_url` from config | `messages=[{role, content: prompt}]`, `system`, `max_tokens`, `temperature`; header `Authorization: Bearer <key>` | ✅ `call_llm` |
| 2 | Cloud embeddings (mem0 embedder, OpenAI-compatible) | `/add` and `/search` — every text that gets embedded or used as a query | `embedder.openai_base_url` + `/embeddings` | `{"input": [<texts>], "model": <name>}`; header `Bearer <key>` | via mem0 runtime; suppressed in `local` mode (local model used instead) |
| 3 | Rerank (`ducky/mem0_runtime.py::rerank`) | hybrid search reranking, verbatim `rank_originals` | `rerank.base_url + /rerank`, or pinned `api.jina.ai` / `api.cohere.com` | `{"query": <query>, "documents": [<candidate texts verbatim>], "top_n", "model"}`; header `Bearer <key>` | ✅ `rerank` |
| 4 | Vision caption (`ducky/pipeline/memory_vision.py`) | `/add` carrying `media_url` or base64 image (unless `_features.vision=false`) | `vision.openai_base_url` | standard OpenAI multimodal payload: the **image itself** (URL or base64) + prompt; header `Bearer <key>` + `User-Agent: aiduMEI-Vision/<version>` | honors `_features.vision=false`; engine_mode gate applies to its LLM path |
| 5 | Decision providers (`ducky/decision.py::_systemone / _cloudflare`) | decision tasks (`memory_type`, `retrieval`, `evidence_assessment`) | `decision.openai_base_url + /systemone`, or Cloudflare `…/accounts/<id>/ai/run/@cf/cloudflare/<model>` | `{"model", "state": <decision state>, "questions": [<task questions>]}`; header `Bearer <key>`; `allow_redirects=False` | ✅ `decision` |
| 6 | Cloud vector store (Qdrant cloud/self-host, mem0 `vector_store`) | every `/add` (upsert) and the cloud leg of `/search` | `vector_store` endpoint from config | memory text + metadata payload + embedding vector; Qdrant API key header if configured | not sent in `local` mode when vector_store is local-disk (`path` in config) |

## What "state" in the decision channel contains

`ducky/decision.py` constructs the decision `state` from memory-store
statistics and task context (counts, score summaries, candidate memories for
the `retrieval` task). If you enable decision tasks, expect **memory content
of the candidates under assessment** to be included in `questions`/`state`.

## Local mode is the hard off-switch

`AIDUMEI_ENGINE_MODE=local` blocks channels 1, 3, 5 at the gate and routes 2
to the local embedding model (`ducky/local_embed.py`), 6 to the local Qdrant
collection (`mem0_local`). Channel 4 is additionally controlled by
`_features.vision` in the config file. Consequence (documented trade-off): no
LLM distillation quality, no cloud reranking — deterministic extraction and
local vector/FTS recall only.

## Key material

API keys are read from `mem0_config_local.json` or `AIDUMEI_LLM_API_KEY` /
`AIDUMEI_EMBEDDER_API_KEY` / `AIDUMEI_RERANKER_API_KEY` (+ vision/decision
equivalents in `.env.example`). Keys are sent only as `Authorization` headers
to the endpoint configured for that channel — never anywhere else, never
logged (`/health` reports configured/provider/model booleans only, never
key or URL values).

## Redirects are refused

All outbound calls made by this codebase pass `allow_redirects=False`
(verified rule following f0.6 W-12: endpoint-swap requires a fresh key for
llm/embedder/rerank/vision). Private-network and link-local metadata endpoints
(169.254.169.254 et al.) are rejected by the endpoint validators.
