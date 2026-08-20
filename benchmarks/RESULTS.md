# 评测结果（v20.0）

## 当前状态：管线已建立，**尚无任何成绩**

v20.0 交付的是**可复现的评测管线**（协议、适配器、schema 校验、smoke
自检、哈希闸门），不是分数。本文件在第一次正式运行完成前只记录管线状态；
任何出现在这里的数字都必须能溯源到一次带完整留证（数据哈希 + 配置 +
JSONL + digest）的正式运行。

| 项 | 状态 |
|---|---|
| 协议冻结（PROTOCOL.md） | ✅ 已冻结（judge/答案模型 prompt+seed 待第一次正式运行前锁定） |
| 适配器（真实 HTTP 契约） | ✅ `benchmarks/adapter.py`，失败分类计数，无占位返回 |
| schema 校验 | ✅ LongMemEval / LoCoMo 装载即校验，类别计数现场生成 |
| smoke 自检 | ✅ 合成 fixture，端到端可跑，digest 可复现 |
| 数据哈希锁定 | ⬜ PENDING——`download.py --register` 后写入 manifest |
| LongMemEval S 正式运行 | ⬜ 未运行 |
| LongMemEval M 正式运行 | ⬜ 未运行 |
| LoCoMo 正式运行 | ⬜ 未运行 |
| 官方 judge 评分 | ⬜ 未运行 |

## 诚实声明

- smoke 输出的 `evidence_recall_diagnostic` 是检索链路诊断，**不是**官方
  指标，不得引用为成绩。
- oracle 文件只作检索上限诊断，永不作为 headline。
- LoCoMo 为 CC BY-NC 4.0：结果仅用于非商业评测比较。
