# Optional decision tasks (f0.4)

The decision channel is opt-in and disabled by default. Customers select the
provider and model; Drex is not mandatory. Without it, the existing engine runs as before.
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

### Customer model selection

| Provider | Default endpoint | Model example |
|---|---|---|
| `nace` | `https://drex.nace.ai/v1` | `drex-v1.5` |
| `typesafe` | `https://api.typesafe.ai/v1` | `jev-1.13.0` |
| `cloudflare` | `https://api.cloudflare.com/client/v4` | `clef` or `clef-flash` |
| `systemone` | Customer-supplied HTTPS endpoint | Customer-supplied model ID |

Examples are defaults for convenience, not mandatory model choices. Every
provider allows an explicit model and HTTPS base URL. `systemone` requires both.
The shared adapter sends `POST <base URL>/systemone` with `model`, `state` and
`questions`, and consumes `answers` containing Choice/confidence or Noul
probabilities. It is not a chat-completions or rerank endpoint adapter.
[TypeSafe API](https://docs.typesafe.ai/api) and [Drex protocol](https://www.nace.ai/drex)
document this request shape. Other protocols require a separate adapter.

For example, replace the provider with `typesafe`, model with `jev-1.13.0`, base
URL with `https://api.typesafe.ai/v1`, and supply that provider's own key. When
switching provider through the config API, send explicit model, endpoint and
key together. Endpoint changes require an explicit key as well; omission or an
empty value cannot silently reuse a stored credential for another destination.
If `AIDUMEI_DECISION_API_KEY` is set, it overrides the JSON key: remove the
override and restart before changing providers in the API, or update the full
service configuration deliberately.

Versioned response model IDs must match the request. Moving `-latest` and
`-preview` aliases may resolve to a versioned ID in the same family; telemetry
records both requested and actual IDs. Pin versions after calibration. A
protocol-compatible model can still have different probability calibration,
Chinese accuracy and latency: test representative workload and set its own
retrieval threshold before relying on it. Classification retains the 0.7
confidence gate and existing fallback. Historical f0.3+ tests used Drex 1.5;
the later local evaluation compares Drex, Jev, Clef and Clef Flash with a
decision-disabled baseline. Its task definitions, scores and limitations are
recorded in [Measured benefits and costs](DECISION_EVALUATION.md).

### Cloudflare Clef

f0.4 fixes memory classification on Clef / Clef Flash: the Cloudflare path sends
one `noul` question per label read from `ducky.memory_types.TYPE_LABELS`, instead
of a `choice` question. The highest valid score is accepted only at >= 0.7;
otherwise classification returns `(None, None)` for the existing caller fallback.
The label list is not hard-coded. Retrieval already uses `noul` and keeps its
protocol; non-Cloudflare classification keeps its existing contract. Credentials
continue to come from settings and are not included in diagnostic output.

The production integration check used six unique inputs covering six labels:
6/6 matched, calls increased by six, failures did not increase, and retrieval
remained usable. This small integration check is not an accuracy benchmark.
[Full f0.4 verification scope](F04_VALIDATION.md).

Cloudflare implements the same typed decision protocol, but its REST response
is wrapped in the Cloudflare API envelope. Configure an Account ID separately;
the service constructs the account-scoped route and unwraps `result` before it
reaches the shared decision pipeline:

```json
{
  "decision": {
    "enabled": true,
    "provider": "cloudflare",
    "config": {
      "model": "clef",
      "openai_base_url": "https://api.cloudflare.com/client/v4",
      "account_id": "<32-hex-character-account-id>",
      "api_key": "",
      "tasks": {"memory_type": true, "retrieval": true}
    }
  }
}
```

Only `clef` and `clef-flash` are accepted for this provider. The Account ID
must be 32 hexadecimal characters; credentials remain masked in `/config`,
telemetry and the console. The current adapter sends no images because aiduMEI
uses Clef for text and structured memory decisions. See the [official Clef
model documentation](https://developers.cloudflare.com/workers-ai/models/clef/)
and [Cloudflare REST API guide](https://developers.cloudflare.com/workers-ai/get-started/rest-api/).

The repository includes `scripts/decision_compare.py`, which runs the same
Chinese memory-type, retrieval-support and score corpus through Drex, Jev and
Clef. It reads keys only from `AIDUMEI_EVAL_*` environment variables, never
accepts a key as a command-line argument, and writes sanitized JSON. The
comparison table belongs in `docs/DECISION_EVALUATION.md` only after a live run
with all three configured models; connectivity or vendor benchmark numbers are
not substituted for aiduMEI measurements.

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

The Hermes hook retains memory-type labels in the injected evidence. Explicit
original-wording requests receive up to 500 characters per VERBATIM item;
other items retain the 120-character budget. Truncated evidence is marked
`[excerpt]`. A VERBATIM label identifies the vault source, not necessarily a
user-authored statement or the complete original message.

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
views and never included in decision telemetry. The shared adapter supports
Nace, TypeSafe and compatible System One services; protocol compatibility does
not establish equivalent quality or calibrated probabilities.

Embedding, chunking, extraction, conflict handling, deletion, core-memory
confirmation, consolidation and federation grants retain their existing owners.
No evidence from the component experiment justified enabling decision writes in
these paths. New types can affect existing type-aware ranking, filters and decay;
verify those effects on representative workload before enabling more tasks.

Display identity: **f0.3++**. Package identity: **0.3.0+decision.2** (PEP 440 local
version). GitHub release title and tag are exactly `f0.3++`. No PyPI upload is
included in this release.
