# Host Agent Integration

aiduMEI is the durable memory layer. The host's native memory remains the short-term conversation layer. Do not copy the same memory into both systems: the host handles current turns and working context; aiduMEI stores facts, raw records, core memory, traces, and long-term evolution.

## Lifecycle

| Host moment | aiduMEI action |
|---|---|
| Before turn | Call `/gate`; if relevant, search or request context and inject once |
| After turn | Write user facts or durable decisions with `/add` |
| Before compression | Save at-risk raw dialogue with `/add/raw` |
| Session end | Call `/session/end` to archive/report the session |
| Restore or migration | Import only durable facts and raw records, not transient working state |

## Scope model

- `user_id`: identity namespace for the human/operator.
- `bank_id`: semantic workspace (for example personal, work, or project).
- Session: conversation-scoped reporting and lifecycle; it is not a tenant replacement.

Write and search must use the same `(user_id, bank_id)`. If a write omitted `bank_id`, it belongs to `default`.

## Avoid double injection

1. Choose one injection source per turn.
2. If the host already injects native memory, use aiduMEI for durable facts only.
3. Keep native working memory out of aiduMEI except when explicitly archiving raw conversation.
4. Verify with `scripts/e2e_smoke.py`: one nonce write must yield one found recall, not repeated context blocks.

## Migrating old memory

1. Export durable facts, preferences, decisions, and raw source records.
2. Import via `/add` or `/add/raw`; include source metadata.
3. Do not import transient state, tool logs, or temporary scratch notes.
4. Run searches from a new session and inspect `/search_trace`.
5. Keep the original export until e2e smoke passes and a backup is verified.

## Non-Hermes hosts

Use the HTTP API as the single integration surface: `/gate`, `/search`, `/add`, `/add/raw`, `/session/start`, and `/session/end`. The Hermes plugin is a convenience wrapper around the same lifecycle, not a required host.
