# Security Policy

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report privately via GitHub's private vulnerability reporting:

- Go to <https://github.com/monkey2jack/aiduMEI/security/advisories/new>

If that channel is unavailable, open a minimal public issue asking for a
private contact — do not include any details of the vulnerability.

We aim to acknowledge reports within 72 hours and to ship a fix or a
documented mitigation decision within 14 days, severity permitting.

## Scope notes

- aiduMEI is a **self-hosted, single-process** memory engine. It is designed
  for loopback or trusted-reverse-proxy deployment with `AIDUMEM_API_TOKEN` /
  UI password enabled. It is **not** a mutually-distrusting multi-tenant SaaS:
  checkpoints / persona banks / observation banks are not on the two-axis
  tenant model. See README's deployment sections before reporting
  configuration-class issues.
- Historical audits and remediations are tracked in
  `docs/SECURITY-AUDIT-LEDGER.md`.

## Security model changes in f0.8 (unreleased)

- **Outbound URL validation (SSRF).** Model, embedding, rerank and vector-store
  endpoints are validated when saved, on `/config/test`, and again right before
  each outbound call. Only http(s); one trailing dot is stripped and anything
  still ending in a dot is refused; all-numeric host forms that do not parse as
  an IP fail closed; the host is DNS-resolved and every address is checked
  (private, loopback, link-local, reserved, multicast, CGNAT, IPv4-mapped /
  compatible / 6to4 / Teredo embedded IPv4). Cloud metadata names and addresses
  are always refused. Literal `127.0.0.1` / `::1` / `localhost` are allowed for
  local model servers unless `AIDUMEI_EGRESS_ALLOW_LOOPBACK=0`; plain http only
  to loopback. LAN model hosts: `AIDUMEI_MODEL_ENDPOINT_ALLOW` (hostnames, IPs or
  CIDRs, comma-separated).
- **`/health` views.** Full payload (git sha, paths, counts, kernel) only for a
  console session or the main token (`AIDUMEM_API_TOKEN`, bound or not); agent
  keys and anonymous callers get the minimal public view.
- **MCP SSE bearer.** The HTTP/SSE transport requires the bearer token on every
  bind, loopback included. stdio is unchanged.
- **Console password retires the env password.** `AIDUMEM_UI_PASSWORD` only
  bootstraps the console; after the owner sets a password in the console the env
  value is ignored. To reset a forgotten console password, delete
  `DATA_DIR/.ui_password_hash` and restart. Empty environment variables no longer
  shadow `.env`; an empty `AIDUMEM_HOST` binds loopback; `0.0.0.0` is not a
  trusted Host header name.
- **Boundary nonces.** Recalled-memory frames (`<memory nonce="…">`), the sandbox
  record markers and the governance review prompt (`<candidate_data nonce="…">`)
  carry a fresh 128-bit nonce on both markers; boundary neutralisation is case-
  and whitespace-insensitive; a candidate containing a forged fence goes to
  human review.
- **Provenance.** `source_type` / `basis` are server-decided. Callers may only
  downgrade to `inferred`; a claimed `direct` is dropped and re-derived; stored
  `direct` values are re-derived on read; `/update` by a bearer caller is stamped
  inferred. A multi-message `/add` is gated, sanitised and labelled per message;
  assistant / tool turns are inferred; secret-only turns are dropped.
- **Limits and read rate limit.** List parameters (`limit`, `top_k`, `k`, `n`,
  `max_results`) are clamped to `[1, AIDUMEI_LIST_LIMIT_MAX]` (default 500) and
  a negative `offset` becomes 0; `/facts` is paginated (`limit`, `offset`,
  `truncated`). Reads (GET/HEAD and POST `*/search`) are rate-limited per
  credential with `AIDUMEI_RATE_READ_PER_MIN` (default 600/min, `0` disables):
  429 with `Retry-After`.
- **Key identity fence.** Self-edit merges and instinct scan / graduation refuse
  an agent key that is not bound to the target identity, even in shadow mode.
  The main token and console sessions keep full access.
- **Forget and redaction.** `/forget` deletes through the same cascade as
  `/delete`, leaves a tombstone (reason `forget`, restorable until
  `AIDUMEI_TOMBSTONE_TTL_DAYS`) and returns its id. Tombstone metadata snapshots
  are redacted by principle 7. Metadata values are redacted with their key as
  context; values that do not look secret (`cookie: chocolate chip`) are kept.

- **Pinned resolution (f0.8+).** The re-check right before each outbound call
  pins the addresses it validated to the calling thread for 30 seconds; the
  HTTP client's own lookup of that host returns only those addresses, so a DNS
  answer that changes between check and connect (rebinding) cannot redirect the
  call. A host that did not resolve at check time is pinned to nothing (the call
  fails). TLS still verifies the original host name. The openai clients inside
  the optional mem0 SDK get the same check through an httpx request hook.
- **Console-session writes and API docs (f0.8+).** Writes authenticated by the
  console cookie must come from the console's own origin (`Sec-Fetch-Site`
  same-origin/none, or an `Origin` equal to the Host); `/docs` and
  `/openapi.json` are served to the console session and the main token only,
  not to per-agent keys.

Known limits: DNS resolution failures are not blocked at check time (the call
then fails on the empty pin); pinning covers synchronous clients that check and
connect in the same thread (all built-in outbound calls are); if the mem0 SDK
hook cannot be attached (SDK internals changed) a warning is logged and only the
start-up configuration check applies. The read rate limit is per process and
per fixed minute window.

## 中文摘要

安全漏洞请走 GitHub 私密漏洞报告（Security → Advisories），不要开公开
issue。72 小时内确认收到，14 天内给修复或书面缓解决定。本项目是自托管
单进程记忆引擎，信任边界（回环 / 可信反代 / 非多租户 SaaS）见 README
部署章节；历史审计见 `docs/SECURITY-AUDIT-LEDGER.md`。

f0.8（未发布）收紧：出站地址 DNS 解析后逐地址校验（SSRF）；`/health` 完整视图只给
控制台与主 token；MCP SSE 回环也要 token；控制台改密后环境变量口令失效；注入围栏开闭
标记带 128 位 nonce；出处由服务端裁定（调用方只能降格为 inferred）；列表 limit 钳制与
读路径按钥匙限流（`AIDUMEI_RATE_READ_PER_MIN`，0 = 关）；钥匙身份围栏覆盖自编辑与
instinct；`/forget` 留墓碑可恢复，元数据按键名语境打码。f0.8+：外呼复检钉住首次解析（防 DNS
重绑定），mem0 SDK 外呼也过同一道校验；控制台会话的写请求必须同源；`/docs` 只给控制台与主 token。
详见上文英文段。
