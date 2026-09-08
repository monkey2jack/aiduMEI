# DEPRECATION.md — 兼容层退役台账（v20.4.0-alpha · P1-5）

> **为什么有这份台账**（六方外审 GPT P1-03 实锤）：本仓兼容层持续累积 ——
> 每一处都写着「兼容」，没有一处写着「何时退役、替代品是谁」。
> 本台账给每处兼容层登记五列：`introduced_at / last_supported_version /
> deprecated_at / planned_removal_version / replacement`。
> **台账与代码同寿命**：tests/test_v20_4_deprecation.py 逐行验证登记的模块
> 真实存在、登记的符号可 AST 解析、五列不留空 —— 台账烂掉，守卫红。

## 退役政策

- **「仅兼容」语义**：不再增强、不再扩展调用面；新调用方一律走 replacement 列的真源。
- **移除窗口**：统一锚定 v21 大版本（对外契约允许破坏的唯一窗口）；到点前由
  当轮任务书逐项复核「调用方已清零」再执行删除。
- **登记义务**：新增兼容门面/旧契约转发层时，必须当 PR 内登记本表；删除时删行。

## 台账

| 模块 | 角色 | introduced_at | last_supported_version | deprecated_at | planned_removal_version | replacement | 登记符号 |
|---|---|---|---|---|---|---|---|
| `ducky/add_speed.py` | /add 高速路径兼容门面（星号 re-export `ducky.speed.*`，旧 import 路径不变） | v9.1（2026-08-01 首提交即在场） | v20.x（当前全支持） | v20.4.0（立账，转入「仅兼容」） | v21（复核调用方清零后移除） | `ducky.speed` 子包（直接 import 真源） | `messages_to_text`、`try_fastpath_text` |
| `ducky/routes_core.py` | HOT 主链路兼容门面（转发 `ducky.hot.register_core_routes`） | v9.1（2026-08-01 首提交即在场） | v20.x（当前全支持） | v20.4.0（立账，转入「仅兼容」） | v21（复核调用方清零后移除） | `ducky.hot`（`register_core_routes` 真源） | `register_core_routes` |
| `ducky/hot/legacy_routes.py` | SQLite Legacy 路由组（/facts 系列旧契约） | v15 世代（2026-08-14 首次出现） | v20.x（当前全支持） | v20.4.0（立账，转入「仅兼容」） | v21（复核调用方清零后移除） | 现行 facts REST（routes_* 组）与 MCP `facts_*` 工具 | `register_legacy_routes` |
| `ducky/memory_salience.py` | 显著性兼容门面（转发 `ducky.pipeline.memory_salience` 与 `ducky.salience` 包；v19.4.1 补齐过转发面） | v11.1 重构（2026-08-01 首提交即在场） | v20.x（当前全支持） | v20.4.0（立账，转入「仅兼容」） | v21（复核调用方清零后移除） | `ducky.salience` 包 / `ducky.pipeline.memory_salience`（直接 import 真源） | `decay_all`、`get_stats` |
| `ducky/extended/` | 扩展路由 + 自动记忆**实装包**（外审 GPT 报告误记名为 `routes_extended.py`，实物是本包；非门面，登记以正视听） | v9.1（2026-08-01 首提交即在场） | v20.x（当前全支持） | 未退役（实装模块，非兼容层） | 不退役（v21 评估 auto_memory 后台环与 routes 是否拆分，拆分再登记） | 无需替代（实装）；如需精细依赖可直接 import `ducky.extended.auto_memory` / `ducky.extended.routes` | `register_extended_routes`、`AUTO_MEMORY_STATE` |

## 历史说明

- 外审报告点名的第五处 `ducky/routes_extended.py` **不存在**，实物为 `ducky/extended/` 包
  （已在台账第 5 行登记）；该笔误记录在《v20.3.4 六方外审评比报告》。
- 「引入版本」列以公开血脉首提交（2026-08-01）与 `git log --diff-filter=A` 为准；
  早于公开血脉的世代（v9.1 / v11.1）以模块文首自述为准。
