# aiduMEI Pi integration

# aiduMEI × Pi 原生接入

这是 aiduMEI 的第六个一等宿主适配，支持 pi 1.6.0+、Node.js 22.19+，Windows / Linux / macOS 共用同一份零依赖 `index.ts`。它不是 Claude Code shell 钩子，而是 pi 进程内的原生扩展。

## 三条自动链路

| 链路 | pi 原生事件 | 行为 |
|---|---|---|
| 召回注入 | `before_agent_start` | 并行读取 CoreMemory 与 `/search`，最多 3 条、每条 300 字；以一次性 nonce 数据围栏注入本轮模型上下文 |
| 对话写入 | `agent_settled` | 只从当前活动会话分支提取真实 user/assistant，忽略工具结果和注入记忆；先写本地持久 outbox，再经 `/add` 落库 |
| 会话萃取 | `session_shutdown` / 空闲计时 | 萃取计划先持久化；`/session/distill` 成功后，摘要必须再经 `/add` 写回，不能只生成不落库 |

写入带 `source=pi-ingest` / `pi-distill`、pi 会话 ID 与消息 entry ID。重复启动、重试和 `/reload` 由持久回执幂等去重；网络故障保留原件重试，永久拒绝转为 `.held`，不删唯一证据。

萃取被服务端节流（`throttled`）时，按响应里的 `retry_after_sec` 一次性重排（最短 60 秒、最长 6 小时），同一任务最多重排 6 次；新一轮对话不会打断已给出的节流窗口，不再每分钟轮询。

## 主动记忆工具

除了三条自动链路，扩展还给模型注册记忆工具，全部只作用于配置里的用户与记忆库，钥匙由扩展从 token 文件读取，**模型看不到钥匙**：

| 工具 | 作用 |
|---|---|
| `memory_search` | 主动查记忆，返回带 id 的结果（nonce 数据围栏） |
| `memory_remember` | 写一条长期记忆，`source=pi-tool` |
| `memory_forget` | 按 id 遗忘一条（墓碑，可由运维恢复）；默认不注册，安装时加 `--allow-forget` 才开启；每个 pi 进程最多 20 次 |

工具自带提示词约束：想不起来先 `memory_search`；长期信息用 `memory_remember`，不写本地记忆文件；**不得读取 token/.env 或用 curl 调 aiduMEI API**。工具同样受工作区白名单约束。

## 现在先升级，暂不部署

本次发布只提供仓库内扩展与工具，不会自动写入 `~/.pi/agent/extensions`，也不会迁移任何现有宿主。以后把嘟嘟迁到 pi 时，在目标机器执行下面的安装即可。

## 安装

先准备 aiduMEI API、用户、记忆库、token 文件和允许写入的工作目录。token 只能放文件，不能放命令行：

```bash
node integrations/pi/manage.mjs install \
  --api http://127.0.0.1:8767 \
  --user dudu \
  --bank default \
  --token-file /绝对路径/aidumei-token \
  --workdir /绝对路径/工作目录 \
  [--allow-forget]
```

建议 token 文件放一把**只绑定该用户的 agent key**（`python scripts/agent_keys.py create --agent pi-dudu --callers dudu --out 文件`，服务端开 `AIDUMEI_AGENT_KEY_ENFORCE=1`），不要放主钥匙。

多个工作目录重复传 `--workdir`。安装器只写这两个位置：

- `~/.pi/agent/extensions/aidumei/`：原生扩展
- `~/.aidumei-pi/config.json`：API、身份、token 文件路径与工作区白名单，不保存 token 正文

已有配置先备份为 `.tar.gz`；重复配置需先卸载。安装后重启 pi 或执行 `/reload`。

## 检查与卸载

```bash
node integrations/pi/manage.mjs check
node integrations/pi/manage.mjs uninstall --yes
```

`check` 检查 pi 版本、扩展文件、配置、token 文件、API 健康、工作区和 held 任务；同目录下其他 `.aidumei-pi*` 状态目录里滞留的任务也会报出（`noLegacyJobs`），避免换状态目录后旧任务无人处理。pi 内也可执行 `/aidumei-check`，只显示脱敏状态。

卸载保留 aiduMEI 记忆、outbox 和回执，便于重装后续传；不会删除服务端数据。

## 隔离测试

```bash
cd integrations/pi
node --test test/*.test.mjs
```

测试环境只用 `AIDUMEI_TEST_URL`、`AIDUMEI_TEST_TOKEN_FILE`、`AIDUMEI_PI_STATE_DIR` 和 `AIDUMEI_PI_ALLOWED_ROOTS`。测试模式不会读取生产配置或生产 token。`AIDUMEI_PI_DEBUG_DIR` 仅在测试模式下保存最终 provider 请求，用于证明 nonce 围栏进入真实模型请求；生产模式禁止该记录。

## 边界

- 默认工作区白名单为空；未显式授权目录时不读、不写。
- 不读取项目 `.env` 或项目 `.pi` 配置，避免克隆仓库改变身份与凭据。
- 不捕获 abandoned branch；不会把分叉里已放弃的回答写成当前事实。
- 召回失败静默放行当前对话，连续失败熔断 60 秒；持久写入失败不会伪装成功。
- 工厂函数不启动后台资源；队列、计时器和锁只在会话事件中建立，并在 `session_shutdown` 关闭。

