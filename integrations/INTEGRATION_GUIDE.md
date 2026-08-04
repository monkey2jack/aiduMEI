# 接入 Hermes Agent

> v15 起有两条路：**A. 官方 MemoryProvider 插件（推荐）** / **B. Shell Hook（兜底）**
> 位置：`<仓库根>/integrations/`

---

## 为什么 v15 加了插件方案

v14 之前只有 Shell Hook 一条路。它能跑，但只有「turn 开头往 user message 后面
追加一段 context」这一个能力，拿不到 Hermes 的任何生命周期钩子——压缩前抢救、
内置 memory 写入镜像、工具调用、备份路径全都没有。更糟的是它靠解析 payload
JSON 字段吃饭，字段一变形就静默返回空，长期不注入也不报错。

官方 `MemoryProvider` 插件把这些全部补上，并且由 Hermes 直接调用 Python 方法，
不再有「脚本解析错字段 → 静默失效」这类事故面。

---

## A. 官方 MemoryProvider 插件（推荐）

### 能力对照

| 钩子 | aiduMEM 端点 | 作用 |
|---|---|---|
| `prefetch` | `/api/core-memory/inject` + `/search` | turn 开头注入常驻块 + 本轮相关检索 |
| `sync_turn` | `/add` | 每轮对话后台归档，不阻塞对话 |
| `on_pre_compress` | `/add` | 压缩前把即将丢掉的轮次先落进长期记忆 |
| `on_memory_write` | `/facts/add` | 镜像 Hermes 内置 MEMORY.md / USER.md 写入 |
| `on_session_end` | `/session/end` | 触发服务端归档与反思 |
| `get_tool_schemas` | `/search` `/add` `/health` | `aidumem_search` / `aidumem_remember` / `aidumem_status` |
| `backup_paths` | — | 数据目录纳入 Hermes 备份流程 |

### 安装

```bash
cp -r integrations/hermes-plugin/aidumem ~/.hermes/plugins/
hermes config set memory.provider aidumem
```

### 配置（全部可选）

```bash
export AIDUMEM_URL=http://127.0.0.1:8767   # 默认回环
export AIDUMEM_USER_ID=default             # 记忆命名空间
export AIDUMEM_DATA_DIR=~/aidumem          # 备份目录
```

也可以走 Hermes 的 provider config（`hermes setup` 里选 aidumem 后填 `url` / `user_id`）。

### 验证

```bash
hermes tools | grep aidumem      # 应看到三个 aidumem_* 工具
curl -s localhost:8767/health | head -c 200
```

再发一条只可能靠长期记忆回答的问题，确认回答里带上了那条事实。

### ⚠️ 安全提示

aiduMEM 服务自身**不做鉴权**。默认监听 `127.0.0.1`，请保持这样。
要跨机访问就在前面挂一层带认证 + TLS 的反向代理，再把 `AIDUMEM_URL` 指过去；
直接把服务暴露到公网等于把全部记忆公开可读可写。

---

## B. Shell Hook（兜底方案）

宿主 Hermes 不方便装插件时用。只有 turn 开头注入这一个能力。

### 数据流

```
用户发消息
   ↓
Hermes (pre_llm_call)
   ↓ JSON payload via stdin
[aidumem-inject.sh]
   ↓ HTTP POST（短超时）
aiduMEM /api/core-memory/inject + /search
   ↓
{"context": "..."} via stdout
   ↓
Hermes 拼到 user message 后面 → LLM
```

### 安装

```bash
mkdir -p ~/.hermes/agent-hooks
cp integrations/aidumem-inject.sh ~/.hermes/agent-hooks/
chmod +x ~/.hermes/agent-hooks/aidumem-inject.sh
```

`~/.hermes/config.yaml` 追加（改前先备份）：

```yaml
hooks:
  pre_llm_call:
    - command: "~/.hermes/agent-hooks/aidumem-inject.sh"
      timeout: 8

hooks_auto_accept: true
```

`hooks_auto_accept: true` 是必须的，否则 shell hook 在启动时会被静默拒绝注册。

### 手动验证

payload 形状必须用真实的那种（`extra.conversation_history`，不是顶层 `messages`）：

```bash
echo '{"hook_event_name":"pre_llm_call","session_id":"s","cwd":"/tmp",
"extra":{"user_message":"用户的生日是哪天",
"conversation_history":[{"role":"user","content":"a"},{"role":"assistant","content":"b"},
{"role":"user","content":"c"},{"role":"assistant","content":"d"},
{"role":"user","content":"e"},{"role":"assistant","content":"f"}]}}' \
  | ~/.hermes/agent-hooks/aidumem-inject.sh
```

会话太短（默认少于 6 条历史）时脚本**故意静默返回 `{}`**——开局几轮没必要注入。
想立刻看到输出就把历史条数加够，或设 `AIDUMEM_MIN_HISTORY=0`。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `AIDUMEM_URL` | `http://127.0.0.1:8767` | 服务地址 |
| `AIDUMEM_USER_ID` | `default` | 记忆命名空间 |
| `AIDUMEM_MIN_HISTORY` | `6` | 少于这个条数不注入 |
| `AIDUMEM_SEARCH_LIMIT` | `5` | 检索条数 |
| `AIDUMEM_TIMEOUT` | `4` | 单次 HTTP 超时（秒） |

改脚本内容后，Hermes 的 mtime 校验会要求重新批准 hook——这是设计如此，不是 bug。

---

## 🆘 回滚

插件方案：

```bash
hermes config set memory.provider ""
rm -rf ~/.hermes/plugins/aidumem
```

Shell Hook 方案：

```bash
rm ~/.hermes/agent-hooks/aidumem-inject.sh
# 手动删掉 config.yaml 里的 hooks: pre_llm_call 那一段
systemctl restart hermes-gateway     # 若以 gateway 方式运行
```

两种方案都不动 aiduMEM 的数据，回滚只是断开接入。

---

## 🚦 风险与性能

| 项 | 等级 | 说明 |
|---|---|---|
| 服务挂掉 | 🟢 低 | 所有调用失败都降级为「无记忆」，不影响对话 |
| 阻塞对话 | 🟢 低 | 读路径短超时（默认 6s），写路径全在后台线程 |
| 注入占 token | 🟡 中 | 注入总量硬上限 4000 字符 |
| 注入了不相关记忆 | 🟡 中 | 相关性闸门 + rerank 双重过滤 |
| 服务无鉴权被外网访问 | 🔴 高 | 必须保持回环或加认证代理 |

每轮多 1–2 次 localhost HTTP 调用（各约几毫秒），LLM 输入多 0–1000 tokens。

---

## ❓ 常见问题

**Q: 两种方案能同时开吗？**
不要。会重复注入，白烧 token。选一个。

**Q: 插件方案要重启吗？**
装完插件和改 `memory.provider` 后要重启一次 Hermes / gateway。

**Q: 为什么查不到我自己项目代号的记忆？**
八成是 `AIDUMEM_ENTITY_KEYWORDS` 没配——相关性闸门会把这类查询判成 no_signal
直接零召回。v15 起启动日志会明确告警，见仓库 `.env.example`。

**Q: 不接入也能用吗？**
可以，直接 `curl` 打 `/search`、`/add`，自己拼进 prompt。
