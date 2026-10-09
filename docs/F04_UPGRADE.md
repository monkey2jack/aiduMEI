# f0.4 upgrade and operations

f0.4 is the 2026-10-09 public release, with package version 0.4.0.
Production acceptance precedes publication. Public documentation changes are
tracked separately from the accepted runtime; verification and retained failures
are described in [validation scope](F04_VALIDATION.md). Back up existing stores
and review the repair and rollback boundaries below before upgrading.

The ingest health probe now distinguishes unobserved host wiring from observed
missing writes. Optional-session MCP/REST traffic leaves an explicit `unknown`
state and warning without marking the service degraded. Strict wiring acceptance
still fails on unknown; session-tagged reads with no session-tagged writes still
degrade. The checker consumes the server threshold. A current successful probe
clears only its own obsolete degradation record; other failures remain visible.

Workspace TTL/LRU eviction emits a structured `workspace_eviction_receipt` in
the existing service log only after its SQLite deletion commits. It binds the
exact owner/bank/ID, body and immutable-row hashes, source digest, and TTL state
or LRU ordering. It does not contain memory text or metadata values. Retain the
original service logs and compare every original cache row separately from
durable memories. These logs are observational, not a new durable journal;
missing logs do not prove an eviction. Scope identifiers are operator-only data.
The in-process cache lock covers both memory eviction and SQL deletion so an
intervening reinsertion cannot be removed by an older eviction.

## Authorization and deployment

Requests use one canonical owner and bank through authentication, authorization
and storage. Configure strict token-to-caller bindings for mutually untrusted
clients; a shared token without bindings remains a trusted single-owner mode.
Read-only query routes require read permission, regardless of HTTP POST.
Governance writes retain the fact owner's scope and the actual proposing caller.

自动审核在外呼期间不持数据库写锁；返回后在短事务内重新核对候选状态、
事实内容、归属和更新候选。已完成人审的决定保持不变，事实已更新或删除的
旧候选标为 `superseded`，保留审计但不再修改事实。该状态无需重写历史记录，
候选查询可用它筛选过期任务。裁决失败回滚，不遗留线程连接写锁。

结构化事实键与 Qdrant 点位 ID 分别处理。不能表示 UUID/uint64 的事实键
没有同名本地点；合法点位仍核对归属，真实后端失败仍使删除保留修复欠账。

Only one API process may own a data directory. The lifetime operating-system lock
also excludes a second launch without a `--workers` argument. Do not delete lock
files to bypass ownership. Stop the existing owner before starting a replacement.

## Durable writes and repair

`mutation_journal.sqlite3` persists request inputs and SDK mutation intent before
acceptance. An SDK write and its local acknowledgement are separate operations.
An interrupted or ambiguous write becomes `repair_required`; it is not silently
retried or called successful. This is not an exactly-once, cross-store transaction.
Other scopes can continue while the affected scope is held for repair.

Startup checks journal integrity before background writers start. Corrupt or
unreadable journals stop startup. Valid pending repair records allow inspection
but make health degraded. Health exposes `mutation_journal_ok`, state counts,
`repair_required`, integrity and `automatic_replay: false`, without memory bodies.
An unreadable journal reports unknown state rather than zero pending writes.
Health opens the journal read-only and never creates a replacement store.
Back up and restore `mutation_journal.identity.json` with the database; its
identity must match the row inside SQLite. Loss of either established artifact
blocks startup and writes. Loss of both, or restoration of a stale matching pair,
requires external deployment/backup inventory to detect. A legitimate pre-identity
v1 journal needs explicit `initialize_journal(adopt_existing=True)` only after
independent verification with all writers stopped; it is not a missing-marker fix.

Repair must inspect the scoped operation and independently verify actual storage
before recording `confirmed_applied` or `confirmed_not_applied` with evidence.
Never infer application from a timeout, replay a non-idempotent SDK call blindly,
or edit the database to erase an inconvenient failure. The local repair utility
is an administrative operation, not an agent-accessible mutation route.

Forgetting coordinates with active writes and clears durable inputs that could
reintroduce forgotten content. Target deletion conservatively clears journal
bodies throughout that owner/bank because another request can quote the same
memory. Existing backup copies remain subject to the operator's retention and
erasure policy; SQLite secure deletion cannot erase older backups.

## Backups and rollback

Back up `.db`, `.sqlite` and `.sqlite3` files with the SQLite online backup API,
including the mutation journal. Keep checksums, database integrity results,
timestamps, code/config/hook manifests and the actual backup location together.
`backup_gate.sh` records the exact member set and includes durable business WAL
and journal identity evidence. SQLite files use the online backup API. Local
files are not a consistent live vector-server snapshot.
Back up each active Qdrant server collection using its snapshot API separately.
Online snapshots across different stores are not an atomic global snapshot.

`restore_gate.sh --dry-run` verifies all three SQLite suffixes, exact member
coverage and checksums. Apply creates only a nonexistent isolated directory,
holds the process lock and verifies copied bytes; it refuses live overlays.
The separate managed drill binds a historical SQLite row to that restored
directory and snapshot. It proves neither API health nor Qdrant server recovery.
A deployment
rollback restores code, hooks and only its own configuration changes. It must
not restore old data over normal writes received during the upgrade window.
Verify service-user read permissions and editable package metadata before the
short API restart; retain current third-party dependencies and provider settings.

The offline installed command `aidumei-mutation-repair --help` describes scoped
inspection and evidence-based resolution. `scripts/wal_rollback_compat.py`
can prepare the current verified WAL for the previous reader only with all
writers stopped and zero WAL/journal debt. It archives the current originals
and preserves file ownership and permissions. This proves WAL-reader format
compatibility only; other schema compatibility requires separate review.

## Decision observations and evidence delivery

Existing per-candidate answer support remains distinct from set completeness.
Retrieval decision tasks share a per-request limit of three provider attempts
and 3000 ms. Cache hits do not spend a provider attempt. Transport timeouts are
cooperative, not a strict preemption guarantee; late decisions are discarded.
Classification has its own policy and does not consume retrieval's budget.

`decision.config.tasks.evidence_assessment` is false by default. When explicitly
enabled it observes the final authorized evidence set after filtering, rerank,
original-text fusion and the output limit. Four independent signals assess
sufficiency, missing links, contradiction and the usefulness of another search.
Only complete coverage with sufficient >= .95 and both missing/contradiction
< .15 yields an observational sufficient verdict. These are experimental policy
thresholds, not calibrated probabilities of correctness.

The observation does not alter rows, skip rerank, suppress original quotations,
trigger deeper searches or write memories. Disabled, unavailable, malformed,
over-budget and truncated inputs retain the recall baseline. `_evidence_assessment`
binds the exact ordered evidence/query/scope to a digest with schema and policy
versions. A changed set invalidates the observation. Storage/observation dates
must not silently become event dates. Long host context uses bounded query-aware
windows, visibly marks omissions and does not claim to quote a whole truncated
record.

The design draws on [Jev-Mem](https://arxiv.org/abs/2609.23986) and the
[authors' implementation](https://github.com/libingzheren/Jev-Mem/tree/7ab0c73c6d8f4f611ad252c1e6ba8083f8df0e44).
Its reported scores are not aiduMEI results. Paired local evaluation must freeze
cases and gold answers in advance, use one attempt per arm, keep failures in the
denominator and never expose gold to the answering model. No new f0.4 speed or
accuracy improvement is claimed here. Four-relation graph exploration remains
a separate design requiring provenance, scope, deletion and rebuild validation.

## Verification evidence

Push gates retain private logs plus an automatically generated receipt, command
exit codes, exact source hashes and log hashes. A source change or failed stage
cannot become PASS by adding a handwritten supplement. Sandbox cron checks may
be explicitly not applicable; that does not prove production maintenance jobs.
Archive and verify required evidence before cleaning temporary workspaces.
# 实机清理边界补充

`delete_all` 同时清理独立 `scenes.db` / `observations.db` 和历史 `facts.db` 中的同名表。场景摘要按用户与库精确删除；旧观察表只有用户列，沿用用户级删除，其他用户及空用户的旧行保留。任一库失败仍为未完成，保留 WAL 待修复，不得将它当作全部擦除成功。MCP `facts_search` 的 `limit` 映射到 HTTP `top_k`；结构化事实通过该入口检索，不能用没有向量点的纯事实测试推断语义检索的召回率。
