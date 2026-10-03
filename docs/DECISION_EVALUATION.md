# f0.3+ decision-channel measurements

**We used Drex 1.5 for testing.** These are small internal experiments with
synthetic memory tasks, not an independent audit, production accuracy estimate,
or official LoCoMo/LongMemEval result. Alternative models are customer choices;
these numbers do not transfer to them.

## Component measurements

The earlier experiment covered 264 tasks with frozen held-out family splits.
Providers, prompts and request shapes differed: latency includes their network
and service behavior, not just model inference on identical hardware.

| Component | Baseline | With Drex | Interpretation |
|---|---|---|---|
| Type classification, 24 held-out memories | Existing LLM 22/24 | 23/24 | One additional correct label; +4.17 percentage points |
| Classification p50 / p95 | 3018 / 11889ms | 665 / 1549ms | p50 about 78.0% lower; small-sample tail estimate |
| Retrieval judgments, 64 held-out questions | 58/64 | 61/64 | Overall improves, but answerable hits fall 48/48 → 45/48 |
| Original-wording hits, earlier indiscriminate filtering | 16/16 | 13/16 | False rejection; this routing was not shipped |
| Rerank nDCG@3 / MRR, 48 answerable questions | BGE 0.8921 / 0.9792 | Drex 0.9063 / 0.9757 | Mixed result; BGE is retained |
| Embedding/index preparation, 24 questions | Rule paragraph chunks: 24/24, MRR 1.0 | Same | No incremental decision-model benefit; embeddings unchanged |
| Extraction precision / completeness | 16/16 / 16/16 | Same | Baseline already correct; no demonstrated gain |
| Relation classification, 40 held-out pairs | LLM 37/40 | Drex 37/40 | No overall gain; destructive decisions not delegated |

In an early 24-question retrieval-to-LLM experiment, strict-format scoring was
24/24 → 17/24. A disclosed post-hoc source/semantic review corrected six false
format failures to 23/24; one real original-wording answer was still lost. Neither
score is evidence of improved final-answer accuracy. Original logs and failed
protocols were retained, and routing changed before the product experiment.

## Same-code product HTTP A/B

Candidate `8df0174b949c310d9e1feb1f3057ec06a72ecc30`: persistent embedded
Qdrant, SQLite, actual HTTP routes, BGE embedding/rerank and Drex. The arms used
the same candidate with decision off/on, isolated data and alternating query
order. Query normalization shared by both arms is not a Drex benefit.

50 questions in 12 families: 32 diagnostic replays and 18 fresh questions from
four new families frozen before execution. Related questions are not independent
samples; the fresh set is small. Counts describe retrieval judgments under this
protocol, not final LLM answers. More representative independent samples would
improve uncertainty estimates, not automatically improve the model's accuracy.

| Metric | Decision off | Decision on |
|---|---:|---:|
| Correct retrieval judgment | 45/50 (90%) | 49/50 (98%) |
| Fresh cases | 17/18 | 18/18 |
| Diagnostic replay | 28/32 | 31/32 |
| Direct questions | 12/12 | 12/12 |
| Paraphrases | 12/12 | 12/12 |
| Original wording | 11/12 | 11/12 |
| Unknown answer correctly empty | 8/12 | 12/12 |
| Mixed quote/summary | 1/1 | 1/1 |
| Unknown-topic quote | 1/1 | 1/1 |
| Retrieval p50 | 417.975ms | 702.10ms |
| Retrieval p95 | 619.26ms | 1363.55ms |

Correctness improves 8 percentage points; unknown-answer rejection improves
33.33 points. Retrieval p50 increases 284.125ms (about 68.0%), p95 increases
744.29ms (about 120.2%). Percentiles use the sorted zero-based
`max(0, int(0.95*n)-1)` index without interpolation. There is no throughput or
concurrent-load benchmark. One common quote failure scored 0.2892 at BGE's 0.3
gate; a downstream decision cannot recover a candidate already discarded.

Observed decision stages: 20 successful calls, 3 cache hits, 8 confident-query
bypasses, 12 original-intent bypasses and 27 empty/inapplicable skips. These are
stages, not 50 query counts; a query may traverse multiple stages. Twelve
classification HTTP calls succeeded, eleven met the confidence gate and one
fell back: coverage, not eleven correct labels.

Protocol SHA256:
`3c9589d127fce1f6962df42c66ae341eb2101b3d84df742fb55545a85364dd1d`.
Full experiment data, failed runs and receipts are retained privately. This
summary does not claim external reproduction.

## Regression controls and remaining limits

Selective routing checks specific attributes even at high rerank scores, while
broad confident questions bypass. Quote/mixed requests bypass added decision
rejection but keep the existing original/rerank gates. Scope filtering precedes
every provider payload. At most 12 candidates are checked per stage; missing
scores and text over 4000 characters preserve the baseline. Successful responses
have a 60-second, 128-entry cache isolated by user, bank, task, model policy,
credential and full content. Two concurrent calls, immediate busy fallback and
a bounded failure circuit prevent unbounded accumulation. No automatic retry or
redirect is used. These controls mitigate overhead; retrieval is still slower.

The host allows a separate six-second search deadline and retains a 1.5-second
core/checkpoint deadline. Provider connect/read timeouts are transport limits,
not a strict total deadline. Original evidence receives a 500-character excerpt
budget; other items retain 120 characters and explicit truncation markers.

Actual host CLI experience used production configuration, installed read hooks
and the live memory API under an isolated profile. Seven hook requests registered
with no hook timeout/failure. Unknown-answer tests returned empty and the model
admitted uncertainty. Birthday vector recall was empty despite core/persona
answering; original candidates were injected but the model still declined to
quote them. These limitations remain documented; a nonempty result or VERBATIM
label alone does not establish a correct final answer. Messaging-gateway user
experience and independent audit were not performed in this round.

Decision writes are limited to the existing memory-type ledger. Embedding,
chunking, extraction, merging, deletion, core-memory confirmation, consolidation
and grants retain their owners. New labels may affect existing type-aware
ranking/filtering/decay. Models and task thresholds require workload-specific
validation; no blanket positive-return claim is made.
