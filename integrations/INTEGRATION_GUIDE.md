# Hermes Gateway 集成指南（Shell Hook 方案）

> **核心方案**：`pre_llm_call` Shell Hook（不是 event hook，也不是 plugin）
> **位置**：`<仓库根>/integrations/`
> **回滚时间**：< 30 秒

---

## 🎯 这是什么

一个 **pre_llm_call Shell Hook**，让 Agent 每轮对话自动带上 aiduMEM 里的相关记忆：

1. Hermes 在每次 LLM turn 之前，把 JSON payload 通过 stdin 喂给 `mem0-inject.sh`
2. 脚本读 `user_message`，调 aiduMEM `/facts/inject-context` API
3. 把 top-K 相关 facts 拼成 `context` 块通过 stdout 返回
4. Hermes 自动把它拼到下一轮 LLM 的 user message 后面

**效果**：对话自动带上长期记忆，不需要每次手动查询。

---

## 🏗️ 架构

```
用户发消息
   ↓
Hermes Gateway (pre_llm_call 事件)
   ↓ JSON payload via stdin
[mem0-inject.sh]  ← 本目录提供
   ↓ HTTP POST (2s timeout)
aiduMEM /facts/inject-context
   ↓
{"context": "## 相关记忆..."} via stdout
   ↓
Hermes 把 context 拼到 user message 后面
   ↓
LLM 调用（自动带记忆）
```

---

## 📦 包含的文件

| 文件 | 用途 |
|---|---|
| `mem0-inject.sh` | Shell Hook 主脚本（约 85 行 bash）|
| `INTEGRATION_GUIDE.md` | 本文档 |
| `config.yaml.snippet` | 要加到 `~/.hermes/config.yaml` 的 hooks block |

---

## ✅ 启用前 Checklist

| # | 检查项 |
|---|---|
| 1 | aiduMEM API 已启动，`/facts/inject-context` 可正常返回 |
| 2 | Shell Hook 脚本单独执行能拿到 context |
| 3 | gateway 当前无其他 `pre_llm_call` hook 冲突 |
| 4 | 已确认可以重启 gateway（重启会中断进行中的会话）|

---

## 🚀 启用步骤

### Step 1：装 Shell Hook 脚本
```bash
mkdir -p ~/.hermes/agent-hooks
cp integrations/mem0-inject.sh ~/.hermes/agent-hooks/
chmod +x ~/.hermes/agent-hooks/mem0-inject.sh
```

### Step 2：注册到 config.yaml
**先备份** `~/.hermes/config.yaml`，然后追加 `hooks:` block：
```yaml
hooks:
  pre_llm_call:
    - command: "~/.hermes/agent-hooks/mem0-inject.sh"
      timeout: 5

hooks_auto_accept: true
```

（`hooks_auto_accept: true` 是必须的，否则 gateway 启动时 shell hook 会被静默拒绝注册）

### Step 3：手动验证脚本能调通 API
```bash
echo '{"hook_event_name":"pre_llm_call","extra":{"user_message":"用户的生日"}}' \
  | ~/.hermes/agent-hooks/mem0-inject.sh
# 期望输出: {"context": "## 📚 相关记忆..."}
```

### Step 4：重启 gateway
```bash
systemctl restart hermes-gateway
sleep 3
systemctl is-active hermes-gateway
```

### Step 5：真实对话测试
发一条问题，其答案只可能来自 aiduMEM 里存的事实，然后确认：

- LLM 回答里带上了那条事实
- 日志里能看到 hook 被调用：`journalctl -u hermes-gateway | grep "mem0-inject\|shell_hooks"`

---

## 🆘 回滚（30 秒内恢复）

### 软回滚（不重启 gateway）
```bash
# 删 config.yaml 里的 hooks block
sed -i '/^hooks:/,/^hooks_auto_accept:/d' ~/.hermes/config.yaml
# 已注册的 hook 仍在内存中，要重启 gateway 才彻底生效
```

### 硬回滚（彻底删除）
```bash
rm ~/.hermes/agent-hooks/mem0-inject.sh
sed -i '/^hooks:/,/^hooks_auto_accept:/d' ~/.hermes/config.yaml
systemctl restart hermes-gateway
```

---

## 🚦 风险评估

| 风险 | 等级 | 缓解 |
|---|---|---|
| API 调用阻塞 LLM | 🟢 低 | 2 秒硬超时 + 异常输出 `{}` |
| API 挂掉 | 🟢 低 | 失败输出 `{}`，subprocess 退出 0 |
| 注入太多 fact 占用 token | 🟡 中 | 默认 5 条 + token 上限 |
| 注入不相关 fact 误导 LLM | 🟡 中 | match_score 阈值 + min_trust 双保险 |
| Gateway 启动时 hook 报错 | 🟢 低 | `hooks_auto_accept: true` |
| 跟其他 hook 冲突 | 🟢 低 | 启用前先查 Checklist 第 3 项 |

---

## 📊 性能影响

- **每条消息多 1 次 HTTP 调用**（localhost，约 5ms）
- **LLM 输入多 0–600 tokens**（取决于匹配度，未命中则零注入）

---

## 🔧 调参

`mem0-inject.sh` 里的参数段：
```python
"k": 5,             # top-K 条数
"min_trust": 0.5,   # 最低 trust 阈值
"max_tokens": 600   # 注入的 token 上限
```

改完不需要重新注册 hook，但**需要重启 gateway**（hook 在 startup 注册）。

---

## ❓ 常见问题

**Q: 跟 event hook 有什么区别？**
- **Event hook**（`agent:start` 等）：只用于日志/告警/外部通知，**不能改 LLM 输入**
- **Plugin hook**：需要 Python plugin + 重启 gateway
- **Shell hook**（本方案）：最简单的「塞 context」方式，subprocess 隔离，失败不影响主流程

**Q: 为什么不用 plugin 方案？**
Plugin 要写 `register_hook` 逻辑，shell hook 一个脚本搞定，进程隔离更稳。

**Q: Hook 会改用户看到的内容吗？**
不会。它只改 LLM 看到的 user message（追加 context），不改 LLM 输出。

**Q: 不启用 hook 能用吗？**
可以。手动调 `curl http://127.0.0.1:8767/facts/inject-context` 拿 context，自己拼进 prompt。
