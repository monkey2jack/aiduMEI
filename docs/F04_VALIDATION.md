# f0.4 validation scope · 2026-10-09

f0.4 is published as package 0.4.0. Its runtime was accepted in production before
the private branch was delivered and the public release was prepared. The public
commit has public-only documentation changes and version publication metadata
updates; every other accepted source file is byte-identical. The only changed
version values are the public-release marker and current lineage description;
package version and all decision/storage implementations remain unchanged. Private repository
ancestry is not part of the public release.

| Evidence | Result and scope |
|---|---|
| Accepted runtime, local and server sandbox suites | Each: 4156 passed, 0 failed/errors/skipped; 8 additional subtests. |
| Production HTTP and host hooks | 91 checks, 24 actual dispatches, passed. |
| Real Hermes CLI | 23/23 accepted: 8 cases executed on the final runtime commit; 15 original cases re-reviewed with relevant runtime files verified unchanged. |
| Clef Flash classification and retrieval | Six unique inputs, six labels, 6/6 expected classifications; calls +6, failures +0; real retrieval passed. Decision files remained identical in the final runtime. A further final-runtime provider call and disabled/scope/empty/451/wrong-model contracts were checked. |
| Backup and recovery | Isolated SQLite and Qdrant restore drills passed. No old data was restored over continuing production writes. |
| Data preservation | All 197 final snapshot differences reconciled with source evidence; zero unexplained differences. |

The two complete runtime test runs passed, but their initial overall gates failed
on stale scanner exceptions for public identifiers. Corrected exact-line review
and the remaining gate stages were completed against the same source commit.
The resulting gate receipts combine those stages; they are not new full test
runs and do not erase the original gate failures.

One CLI case initially failed because the operations evaluator did not recognize
legitimate host `tool_search` discovery. The evaluator was corrected with nine
positive/negative checks; the original stream, persisted tool calls, scoped HTTP
and effects were re-reviewed. That case was not executed a second time. Daily
production uses shell hooks; the candidate plugin was exercised through an
isolated real Hermes CLI profile against the production API. This does not claim
that the plugin is installed as the daily host integration.

In one quotation case, stored/API/tool text remained exact but the host changed
one fullwidth comma to an ASCII comma. The host answer is not claimed to be a
literal transcription. Test-body cleanup passed after a retained partial result
was reconciled; body-free audit metadata and required backup evidence remain.

The original strict data-diff failures remain in the private evidence archive.
Reconciliation separately proves normal fact updates, evolution links, cache
evictions and the installed recent-message retention behavior. Online snapshots
are not a global atomic snapshot, and durable intent is not an exactly-once
cross-store transaction.

The original Jev HTTP 451 remains an unavailable-provider observation. No
geographic restriction was bypassed. f0.4 validates adapter behavior and normal
use; it introduces no comparative decision-model accuracy or latency ranking.
Historical measurements keep their original scope and limitations.

Private source logs, credentials, user content and deployment identifiers are
excluded from public artifacts. The public release includes wheel/sdist assets
and checksums; GitHub is the distribution channel for this release.
