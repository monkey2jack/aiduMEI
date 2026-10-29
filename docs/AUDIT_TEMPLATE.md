# 外部审计快照说明模板（f0.7 P2-5）

每次把代码放进临时审计仓时，复制本文件为审计仓根目录的 `AUDIT.md`，填好 `<…>` 部分。
写法纪律：**能在审计仓里复现的数字给命令；不能复现的单列成「声称」，并写清口径。**

## 1. 这是什么

- 项目：aiduMEI，自托管 Python/FastAPI 长期记忆引擎（CC 钩子：`/add` `/search` `/update` `/delete` `/session/distill` `/api/core-memory`）。
- 版本：`<发版名>`（`SERVICE_VERSION <x.y.z>`）。快照 = 私有仓提交 `<hash>` 的文件树，外加第 2 节的改动。
- 生产内核：`AIDUMEI_MEMORY_KERNEL=native`。

## 2. 与源提交的差异（导出时固定做的三件事）

| 改动 | 做法 |
|---|---|
| 真实回归集换成虚构集 | `python -m benchmarks.build_public_regression --out benchmarks/regression_v3.json --replace-real` |
| 两份基线按虚构集重录 | `python -m benchmarks.regression --out /tmp/r.json` 与 `AIDUMEI_MEMORY_KERNEL=native python -m benchmarks.regression --out /tmp/rn.json`，再写回 `benchmarks/regression_v3_baseline.json` / `regression_v3_baseline_native.json` |
| 私人名词扫描 | 私有词表只放在未入仓的 `benchmarks/.scan_words`；导出前跑 `python -m pytest -q tests/test_regression_v3.py` 确认零残留 |

## 3. 如何复现

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-mem0.txt -r requirements-dev.txt
ulimit -n 4096
python -m pytest -q                                                       # 全量测试
python -m benchmarks.regression --baseline benchmarks/regression_v3_baseline.json            # mem0 内核，逐类不降
AIDUMEI_MEMORY_KERNEL=native python -m benchmarks.regression \
  --baseline benchmarks/regression_v3_baseline_native.json                                   # 生产 native 内核，逐类不降
AIDUMEI_MEMORY_KERNEL=native python -m benchmarks.rank_raw_vs_fact                          # 原话 vs 提炼事实排序
```

注意：`--baseline` 是对**比**基线，不会改写基线文件；基线文件自身 score 与 pass/n 不符会直接判红。

## 4. 不能在审计仓复现的「声称」（口径写清）

- 真实回归集 v3 各类得分（私有集，不公开）。
- 路由数：数**根路径**白名单（CC 钩子 + `/health` 等）与**全站**（含 `/api/*` 控制台、`/ui`）要分开报——f0.6+++ 时根路径 11 条、全站 48 条，旧口径「168 → 11」只数了根路径，已勘误。
- rankfix：生产真实话题 10 题里「提炼事实排第一」**9/10**（旧报 10/10 有误，以复跑为准）。
- 生产数据副本上的迁移/删除复核结果（只读副本，结论附在任务书）。

## 5. 已知问题（不需要再报）

`<从任务书「未完成 / 申报残留」一节复制>`

## 6. 报告格式

编号、严重级别（P0 致命 / P1 严重 / P2 一般 / P3 建议）、文件与行号、复现命令、实际 vs 预期、修复建议；没有复现证据的推测标「推测」。
