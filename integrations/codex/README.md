# 接入 Codex CLI

> **Canonical contract**: [`docs/AGENT_INTEGRATION.md`](../../docs/AGENT_INTEGRATION.md) 是接入范围、生命周期、鉴权与端点契约的唯一真源。本文只提供 Codex 特有的安装步骤，并指向该 canonical 指南。

> Codex CLI（≥ 0.163）原生支持 lifecycle hooks，事件模型与 Claude Code 对齐：
> `UserPromptSubmit`（读线）/ `Stop`（写线）/ `SessionEnd`（萃取线）。
> 因此 aiduMEI 的三线架构可以**原样复用**，无需轮询数据库、无需解析 transcript。

---

## 为什么是 hooks 而不是轮询

Codex 的会话历史落在 `~/.codex/thread_history_1.sqlite`（WAL 模式、schema 随版本演进），
轮询方案要自己维护 watermark、处理并发读、跟随 schema 漂移 —— 全是静默失败温床。
官方 hooks 在正确的时机把正确的 payload 推给你：

| 线 | Codex 事件 | 脚本 | aiduMEI 端点 | 作用 |
|---|---|---|---|---|
| 读线 | `UserPromptSubmit` | `aidumem-codex-inject.sh` | `/api/core-memory` + `/search` | 每轮**之前**把相关记忆喂给模型 |
| 写线 | `Stop` | `aidumem-codex-ingest.sh` | `/add` | 每轮**之后**把这一轮存回去 |
| 萃取线 | `SessionEnd` | `aidumem-codex-distill.sh` | `/session/distill` → `/add` | 会话结束时提炼「这一程」精华 |

三线同租户同凭据，身份解析优先级与 Hermes / Claude Code 版逐行同源：
环境变量 → `.env` 链（`AIDUMEM_ENV_FILE` / `AIDUMEM_HOME/.env` / `~/.aidumem/.env` / 仓库 `.env`）→ `default`。
真源**不硬编码任何租户名或部署路径**，一切私密身份经 `.env` 注入。

---

## 安装（3 步）

### 1. MCP Server（41 个记忆工具）

合并 [`config.toml.snippet`](config.toml.snippet) 里的 `[mcp_servers.aidumei]` 段到 `~/.codex/config.toml`，
把 `/path/to/aiduMEI` 换成你的安装目录。**必须用 venv 里的 python**（系统 `python3` 缺 `httpx`/`mcp` 依赖）：

```toml
[mcp_servers.aidumei]
command = "/path/to/aiduMEI/venv/bin/python"
args = ["/path/to/aiduMEI/mcp_server.py"]
env = { AIDUMEM_CONFIG_FILE = "/path/to/aiduMEI/mem0_config_local.json", AIDUMEM_API_TOKEN = "<your-token>", AIDUMEM_USER_ID = "<your-user-id>" }
```

验证：`codex mcp list` 应出现 `aidumei`；或在对话里让模型调用 `mem_health`。

### 2. 三线钩子

把 [`hooks.json.snippet`](hooks.json.snippet) 复制为 `~/.codex/hooks.json`，替换命令里的路径：

```bash
cp integrations/codex/hooks.json.snippet ~/.codex/hooks.json
# 编辑 ~/.codex/hooks.json，把 /path/to/aiduMEI 换成真实路径
```

> **不要**在同一配置层同时放 `hooks.json` 和 inline `[hooks]` —— Codex 会合并两者并在启动时告警。
> 一层只用一种表示。

### 3. 信任钩子

Codex 的非 managed hook **必须先审阅并信任才会运行**（按 hook 定义的当前 hash 记录信任，
改动后需重新信任）。两种方式：

- **交互式**：在 Codex TUI 里输入 `/hooks`，审阅并 trust 三个钩子。
- **无头自动化**（aiduCAT bridge / CI / cron）：没有 TUI 可审阅，须由宿主在 `codex exec`
  传 `--dangerously-bypass-hook-trust`。
  - **aiduCAT 用户**：在 profile 配置里设 `codex.bypassHookTrust = true` 即可，
    aiduCAT 会自动把该 flag 加进 argv（见 `src/agent/codex/argv.ts`）。
  - 其他宿主：自行在 spawn codex 时追加该 flag。

> ⚠️ `dangerously_bypass_hook_trust` 写在 `config.toml` 里**不生效**，只有 CLI flag 有效（实测 0.163.0）。

---

## 配置（全部可选）

| 变量 | 默认 | 说明 |
|---|---|---|
| `AIDUMEM_URL` | `http://127.0.0.1:8767` | aiduMEI 服务地址 |
| `AIDUMEM_USER_ID` | `.env` 兜底 → `default` | 记忆归属租户 |
| `AIDUMEM_DEFAULT_USER_ID` | — | `AIDUMEM_USER_ID` 缺省时的回落值 |
| `AIDUMEI_BANK_ID` | `.env` 兜底 → `default` | 记忆库 |
| `AIDUMEM_API_TOKEN` | `.env` 兜底 | 服务门禁 token |
| `AIDUMEM_ENV_FILE` | — | `.env` 路径候选之一 |
| `AIDUMEM_LOG_DIR` | `~/.aidumem/logs` | 写线失败日志目录 |
| `AIDUMEI_CODEX_SESSION_PREFIX` | `codex-` | 会话 id 前缀（**三线必须同值**） |
| `AIDUMEI_CODEX_SPOOL_DIR` | `${TMPDIR:-/tmp}/aidumem-codex-spool` | 读写线 turn 配对目录 |
| `AIDUMEI_INSTALL_DIR` | 按脚本位置推导 | 萃取线找真源用 |

---

## 验收（装完必做）

```bash
# 1) 三线真源语法与自检
bash -n integrations/codex/aidumem-codex-*.sh
AIDUMEM_USER_ID=<你的uid> bash integrations/codex/aidumem-codex-inject.sh --selftest
AIDUMEM_USER_ID=<你的uid> bash integrations/codex/aidumem-codex-ingest.sh --selftest

# 2) 端到端：真聊几轮后，确认写线带上了 session_id、萃取线出了精华
#    （在 Codex 里正常对话几轮，然后：）
curl -s -H "Authorization: Bearer $TOKEN" -X POST "$URL/search" \
  -d '{"query":"<本程关键词>","caller_user_id":"<uid>"}' | grep -E 'distill:|codex-ingest'

# 3) 健康探针
curl -s -H "Authorization: Bearer $TOKEN" "$URL/health" | python3 -m json.tool | grep -E 'ingest_liveness_ok|distill_liveness_ok'
```

> **萃取线接通的命根子**：写线沉淀时 `metadata._origin_session_id` 必须与萃取线圈定用的
> `session_id` **逐字一致**（都带 `codex-` 前缀）。差一个字符，`verbatim_turns` 圈不到来源，
> 每轮记忆照常进、探针全绿，精华却永远不产生 —— 这是静默失败。

---

## 与 Claude Code 版的差异

| 点 | Claude Code | Codex CLI |
|---|---|---|
| 写线触发事件 | `Stop` | `Stop`（同名，payload 多 `last_assistant_message`） |
| 用户原话来源 | `transcript_path` JSONL | **spool 配对**（读线暂存 prompt，写线按 `turn_id` 取）；transcript 仅兜底 |
| 萃取线超时 | 宿主自定 | **1s 默认 / 3s 上限，且始终同步**（`async:true` 无效）→ 真源内部 setsid 后台化 |
| hook 信任 | `settings.json` 注册即生效 | 需 `/hooks` 审阅信任，或 `--dangerously-bypass-hook-trust` |
| 会话前缀 | `cc-` | `codex-` |

写线用 spool 而非 transcript 的原因：Codex 官方明确声明
「`transcript_path` points to a chat transcript for convenience, but the transcript format
isn't a stable interface for hooks and may change over time」。依赖它会重蹈
「字段一变形就静默返回空」的覆辙。spool 由我们自己的读线写入、写线读取，
格式完全自控；transcript 仅作 spool 缺失时的尽力兜底。

---

## 🆘 回滚

```bash
rm ~/.codex/hooks.json
# 手动删掉 ~/.codex/config.toml 里的 [mcp_servers.aidumei] 段
```

回滚不动 aiduMEI 的数据，只是断开接入。

---

## 🚦 风险与性能

| 项 | 等级 | 说明 |
|---|---|---|
| 服务挂掉 | 🟢 低 | 读线探活 1s 超时 + 5s 熔断；写线/萃取线异步后台，失败只记日志 |
| 阻塞对话 | 🟢 低 | 读线同步但短超时；写线 POST 在后台子进程；萃取线真源 setsid 立即返回 |
| 注入占 token | 🟡 中 | CoreMemory ≤3 块 ×200 字 + 检索 ≤3 条 ×150 字，硬上限约 1000 字 |
| SessionEnd 超时 | 🟡 中 | 上限 3s；真源后台化保证本层 <100ms 返回，提炼在后台完成 |
| hook 未信任 | 🔴 高 | **静默不运行**，探针全绿但记忆不进 —— 装完务必跑 `--selftest` + 端到端验收 |

每轮多 1–2 次 localhost HTTP 调用（各约几毫秒），LLM 输入多 0–1000 tokens。
