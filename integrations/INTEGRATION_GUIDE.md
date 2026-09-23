# 接入 Hermes Agent

> **Canonical contract**: `docs/AGENT_INTEGRATION.md` is the single source of truth for integration scope, lifecycle, authentication, and endpoint contracts. This document only provides Hermes-specific installation steps and points to that canonical guide.

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

| 钩子 | aiduMEI 端点 | 作用 |
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

aiduMEI 默认仅监听回环；设置 API token 或 UI 口令后接口会强制鉴权。跨机访问必须配置凭据并前置 TLS 反代。
要跨机访问就在前面挂一层带认证 + TLS 的反向代理，再把 `AIDUMEM_URL` 指过去；
直接把服务暴露到公网等于把全部记忆公开可读可写。

---

## B. Shell Hook（兜底方案）

宿主 Hermes 不方便装插件时用。**两个脚本，两个挂点，缺一不可。**

### ⚠️ 先读这段：装一半等于没装

记忆是一条闭合电路：**读线**把旧记忆喂给模型，**写线**把新对话存回去。
两条线的漏装代价完全不对称——

| | 脚本 | 挂点 | 漏了会怎样 |
|---|---|---|---|
| **读线** | `aidumem-inject.sh` | `pre_llm_call` | 几分钟内就发现：模型明显不记事 |
| **写线** | `aidumem-ingest.sh` | `post_llm_call` | **所有指标都正常**，几周后才发现新记忆一条没进 |
| **萃取线** | `aidumem-distill.sh` | `on_session_end` | 记忆照常进，只是永远没有「这一程最值得记住的是什么」那一层 |

v21.2.0 之前，本文件这一段**只写了 `pre_llm_call`**，于是照它装的部署
每轮都在读、从来没写过，持续了很久才被人工审计翻数据库发现。
它难发现是因为每个绿灯都还绿着：检索有结果（旧记忆还在）、`/health` 全绿
（库里的记忆确实健康）、集成自检通过（它自己调 `/add` 自己调 `/search`，
测的是被集成方不是集成本身）。唯一症状是「新记忆再也没进来过」，
而当时没有任何一个探针在问这个问题。

现在有了：`/health` 的 `ingest_liveness_ok` 把「读」和「写」放在一起比，
**有检索却零写入即判降级**；`scripts/check_ingest_wiring.py` 可随时单独问一句
「你在读，那你在写吗」。

### 数据流

```
用户发消息
   ↓
Hermes (pre_llm_call)  ← 读线
   ↓ JSON payload via stdin
[aidumem-inject.sh]
   ↓ HTTP POST（短超时）
aiduMEI /api/core-memory/inject + /search
   ↓
{"context": "..."} via stdout
   ↓
Hermes 拼到 user message 后面 → LLM
   ↓
LLM 回答完，工具循环结束
   ↓
Hermes (post_llm_call)  ← 写线
   ↓ JSON payload via stdin（含 user_message + assistant_response）
[aidumem-ingest.sh]
   ↓ HTTP POST /add（带 _origin_session_id / _origin_turn）
aiduMEI 落库 → 下一次 pre_llm_call 就能搜到
```

### 安装

```bash
mkdir -p ~/.hermes/agent-hooks
cp integrations/aidumem-inject.sh integrations/aidumem-ingest.sh \
   integrations/aidumem-distill.sh ~/.hermes/agent-hooks/
chmod +x ~/.hermes/agent-hooks/aidumem-{inject,ingest,distill}.sh
```

> ⚠️ **升级时必须重新部署钩子。** 钩子是**拷贝不是软链**——只更新仓库代码，宿主执行的仍是旧文件，且不报任何错（静默失效）。
>
> 并且**先读 `config.yaml` 认路径，别照抄上面的文件名**：早期安装可能用了别的文件名（例如 `mem0-inject.sh`），宿主只认 `config.yaml` 里写的那一个。
>
> ```bash
> # 1) 问宿主：你到底在调哪个文件？
> grep -A2 -E "pre_llm_call|post_llm_call|on_session_end" ~/.hermes/config.yaml
>
> # 2) 按它说的那个路径部署（下面 $DST 换成上一步读到的真实路径）
> install -m 755 integrations/aidumem-inject.sh "$DST"
>
> # 3) 核验：宿主加载的那个文件与仓库同 md5，才算部署到位
> md5sum "$DST" integrations/aidumem-inject.sh
> ```
>
> **验证必须打在宿主真正调用的那个文件上——验仓库文件等于没验。**

`~/.hermes/config.yaml` 追加（改前先备份）：

```yaml
hooks:
  pre_llm_call:                                    # 读线
    - command: "~/.hermes/agent-hooks/aidumem-inject.sh"
      timeout: 8
  post_llm_call:                                   # 写线 —— 别漏
    - command: "~/.hermes/agent-hooks/aidumem-ingest.sh"
      timeout: 10
  on_session_end:                                     # 萃取线 —— 这一程的精华
    - command: "~/.hermes/agent-hooks/aidumem-distill.sh"
      timeout: 40

hooks_auto_accept: true
```

`hooks_auto_accept: true` 是必须的，否则 shell hook 在启动时会被静默拒绝注册。

### 装完必做的三步验收

```bash
~/.hermes/agent-hooks/aidumem-inject.sh --selftest    # 读线：真打一次 /search
~/.hermes/agent-hooks/aidumem-ingest.sh --selftest    # 写线：真写一条再回读
~/.hermes/agent-hooks/aidumem-distill.sh --selftest   # 萃取线：端点在场
# 然后真聊 5 轮，再问一次接线：
python3 scripts/check_ingest_wiring.py                # 退出码非 0 即接线有问题
```

第三步不能省。前两步证明的是「脚本能跑通」，只有第三步证明「宿主真的在调它」——
v21.2.0 之前那次事故里，脚本一直是好的，没被挂上而已。

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

写线（`aidumem-ingest.sh`）额外认这几个，其余键与读线共用同一套
（**故意共用**：两条线必须解析出同一个租户和同一份凭据，否则会出现
「写进了 A、读的是 B」这种两边各自正常、合起来失忆的故障）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `AIDUMEI_INGEST_TIMEOUT` | `6.0` | 写比读慢，超时给足 |
| `AIDUMEI_INGEST_MIN_CHARS` | `8` | 用户消息短于这个字数不写 |

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
rm ~/.hermes/agent-hooks/aidumem-{inject,ingest,distill}.sh
# 手动删掉 config.yaml 里 hooks 下的 pre_llm_call / post_llm_call / on_session_end 三段
systemctl restart hermes-gateway     # 若以 gateway 方式运行
```

两种方案都不动 aiduMEI 的数据，回滚只是断开接入。

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
