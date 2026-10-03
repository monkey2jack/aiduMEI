# Optional decision tasks (f0.3+)

The decision channel is opt-in. Without it, the existing engine runs as before.
Rerank continues to rank candidates and reject low relevance. The decision model
checks direct answer support for ambiguous candidates and can classify new memory
types. It does not produce embeddings or replace the extraction LLM.

Add this section to `mem0_config_local.json`, using your own key (or set
`AIDUMEI_DECISION_API_KEY` in the service environment):

```json
{
  "decision": {
    "enabled": true,
    "provider": "nace",
    "config": {
      "model": "drex-v1.5",
      "openai_base_url": "https://drex.nace.ai/v1",
      "api_key": "",
      "tasks": {"memory_type": true, "retrieval": true},
      "users": [],
      "mode": "auto",
      "threshold": 0.6,
      "timeout_ms": 2000
    }
  }
}
```

After this initial configuration, ordinary writes and searches use the enabled
tasks automatically. No per-query switch, manual classification job, or service
restart is needed for JSON configuration changes. Environment changes require a
restart. Administrators can also use `PUT /config/decision?caller=<admin>` with
the same section. Missing switches preserve existing settings; an empty API key
on save preserves the stored key. Send the complete `tasks` map when changing it.
`users` restricts the channel to specific user IDs; empty means all users.

`auto` checks up to 12 ambiguous candidates per stage, after scope filtering and
rerank, before final result slots. A rerank score of at least 0.85 skips the new
check for broad queries; questions about a specific attribute still require
support because topic similarity alone does not establish its value. `always`
checks all eligible candidates, subject to the same cap.
Successful support scores below 0.6 are rejected. Scores absent from a partial
response remain unknown and preserve the baseline. Long text (over 4000
characters) and candidates without a fresh rerank score preserve the baseline.
Explicit original-wording requests (including mixed quote/summary requests)
bypass decision rejection and retain the existing original/rerank checks.
These policies preserve recall but do not prove that every returned item answers
the question, or guarantee that false positives reach zero.

Memory type classification uses the six existing types. A valid provider
confidence of at least 0.7 is required; otherwise existing LLM/rules apply.
Classification runs synchronously in the existing write transaction sequence,
with a bounded provider request, so there are no delayed decision jobs that can
overwrite newer labels. Provider confidence is not calibrated accuracy.

Provider calls have a connection timeout capped at two seconds and the configured read timeout;
these are transport deadlines, not a guaranteed end-to-end request budget.
The Hermes read hook automatically allows six seconds for `/search` in this
build, while CoreMemory/Checkpoint retain their 1.5-second default. This fixes
the former 1.5-second search timeout cutting off healthy decision calls.
`AIDUMEI_SEARCH_TIMEOUT` overrides the search deadline; an explicitly supplied
legacy `AIDUMEM_TIMEOUT` remains the fallback for both paths. Provider failures
can still exceed the outer deadline, which is reported by the hook; a custom
Hermes lifecycle timeout must also accommodate the complete hook.
At most two decision calls run concurrently. A busy channel immediately falls
back. Three failures open a 30-second circuit; the next request probes recovery.
Only successful responses are cached (60 seconds, at most 128 entries), keyed by
task, user, bank, policy, credential fingerprint, query and full candidate text.
Failures never fabricate support scores. Health reports configuration and process
counters without calling the provider. Search exposes `_decision.stages`, including
actual calls, cache hits, skips, fallback, valid scores and rejected candidates.

The `local` engine mode makes no decision provider calls, even if the channel is
enabled. Enabling a cloud decision channel sends scoped query/candidate text or
the newly classified memory to the configured provider. Keys are masked in API
views and never included in decision telemetry. The adapter registry currently
implements Nace/Drex 1.5 only; other decision protocols need their own adapter and
verification before they can be selected.

Embedding, chunking, extraction, conflict handling, deletion, core-memory
confirmation, consolidation and federation grants retain their existing owners.
No evidence from the component experiment justified enabling decision writes in
these paths. New types can affect existing type-aware ranking, filters and decay;
verify those effects on representative workload before enabling more tasks.

Display identity: **f0.3+**. Package identity: **0.3.0+decision.1** (PEP 440 local
version). This build is not a new public Release or PyPI upload.
