"""benchmarks.run — 评测编排：smoke（可重复自检）与 formal（正式运行）。

诚实边界（v20.0 权威方案 §4.4）：

- smoke 用**合成 fixture**（官方 schema 形状、安全假内容）验证管线端到端
  可用与可重复。smoke 的诊断分只衡量「检索到证据会话」的召回诊断，
  **不是** LongMemEval/LoCoMo 官方指标，不得当成绩宣传。
- 可重复性分两档（v20 修订，PROTOCOL.md §5 有修订记录）：
  **G3a** 默认模式（LLM 在环）只断言结构不变量一致；**G3b**
  ``--deterministic``（``infer=false``，LLM 出环）要求 digest bit 相同。
  含远程 LLM 的链路上「两遍字节相同」原理上不可达，把它写成通用闸门
  等于写一条永远红的断言。
- 无证据的弃答/对抗题在召回诊断上记 **N/A（null）而非 0.0**，汇总时
  跳过而不是拿 0 充数——用不适用的题惩罚检索能力是自欺。
- formal 必须先有完整的哈希清单（PROTOCOL.md 锁定 + manifest 文件），
  数据文件 SHA-256 与 manifest 对得上才许开跑；对不上直接拒绝，
  没有「先跑了再说」。
- 只有检索返回的证据才可交给答案模型；未检索到证据时不作答（abstain），
  这一条有负向测试钉死。
- 每次运行落 JSONL 原始记录 + summary（含配置、失败分类统计、digest）。
  digest 排除时间戳/延迟/request_id 等波动字段，只对确定性内容取哈希。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any

from benchmarks.adapter import AdapterError, AiduMEIBenchmarkAdapter
from benchmarks.schemas import validate_locomo, validate_longmemeval

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
MANIFEST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data_manifest.json"
)

# LoCoMo 会话时间形如 '1:56 pm on 8 May, 2023'
_LOCOMO_TIME_FMTS = ("%I:%M %p on %d %B, %Y", "%d %B, %Y")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _stable_digest(records: list[dict], config: dict) -> str:
    """对确定性内容取哈希：剔除时间戳/延迟/request_id 类波动字段。

    v20 修（G3）：此前只剥了顶层那 7 个字段，**嵌套在检索结果里的簿记
    字段一个都没剥** —— ``id``/``memory_id`` 是每次新建的 UUID，
    ``hash`` 是内容哈希，``created_at``/``updated_at``/``recorded_at``
    是墙钟时间。它们按构造每遍必变、零评测信号，等于让 digest 永远不
    可能相等；这违背了 PROTOCOL.md 自己写明的「剔除波动字段」意图。

    语义字段不受影响：本管线的语义标识叫 ``question_id``/``sample_id``/
    ``case_id``/``fixture_sha256``/``data_sha256``，没有一个叫裸 ``id``
    或裸 ``hash``；``data_report`` 里也没有这两个键。

    诚实边界（重要）：补齐剥离**不足以**让 digest 相等。残留差异是
    LLM 抽取出的 ``memory`` 正文本身，以及由它派生的 ``score``/
    ``_hybrid_score``/``_bm25_rank``。含远程 LLM 的链路上「两遍 bit
    相同」原理上不可达 —— 这就是 G3 拆成 G3a/G3b 的原因，见
    PROTOCOL.md §5 与其修订记录。

    模型派生浮点（v20 修订 2，实测所迫）：``score`` / ``_hybrid_score``
    / ``_time_decay`` 也一并剔除。理由不是「不好过就不查」——
    - ``score`` 是**远程 embedding 服务**的相似度输出。实测 G3b
      （LLM 已出环）两遍仍差在第 4 位小数（最大实测 |Δ|≈5.9e-4）：
      远程神经推理与 LLM 同类，不保证逐字节可复现。
    - ``_time_decay`` 是墙钟时间的函数，按构造每遍必变。
    - 四舍五入不是解法：观测到的抖动量级足以在边界上翻掉小数第 3 位，
      那会造出一条**偶发红**的闸门，比没有闸门更坏（它教人忽略红灯）。
    被剔除的只有数值，**排序与集合仍在 digest 内**：命中哪几条、按什么
    次序、正文是什么、命中了哪些证据——任一改变都照样把 digest 打红。
    数值本身改由 ``compare_runs.py`` 以显式容差检查，并如实报出实测
    噪声地板，而不是折叠成一个布尔。
    """
    volatile = {"request_id", "request_ids", "latency_ms", "started_at",
                "finished_at", "duration_s", "raw",
                # 嵌套簿记字段（检索结果 item 及其 metadata 内部）
                "id", "memory_id", "hash",
                "created_at", "updated_at", "recorded_at",
                # 模型派生浮点：远程 embedding 输出 + 墙钟衰减
                "score", "_hybrid_score", "_time_decay"}

    def clean(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in sorted(obj.items())
                    if k not in volatile}
        if isinstance(obj, list):
            return [clean(v) for v in obj]
        return obj

    payload = json.dumps(
        {"config": clean(config), "records": clean(records)},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _norm_text(text: str) -> str:
    """原文归一化：只压空白、统一大小写。**不做同义改写、不做截断。**

    这里刻意保持「几乎等同于原串」——见 ``_match_evidence`` 的说明：
    这是身份判定，不是语义相似度打分。
    """
    return " ".join(str(text).split()).casefold()


def _match_evidence(
    results: list,
    evidence: list[str],
    text_to_id: dict[str, set[str]],
    *,
    meta_key: str = "bench_dia_id",
) -> tuple[list[str], dict[str, str]]:
    """判定召回结果命中了哪些证据，返回 (命中列表, 每条命中的判据)。

    为什么需要两种判据（v20 修订 2，实测所迫）：``/search`` 的两条召回
    路径**回来的形状不一样**——

    * 抽取路径（LLM 写出的记忆）带 ``metadata``，其中有我们灌进去的
      ``bench_dia_id``，可以精确回指是哪一轮。
    * verbatim 路径（``_recall_path="like"``、``_verbatim=True``）回来的
      item **根本没有 metadata 字典**：``verbatim_turns`` 表里就没有
      metadata 列，自定义元数据从未落库。它带回来的是**逐字原文**。

    于是只认元数据会造成「假红」：实测生产通路（``infer=true``）下
    LoCoMo 有一题召回回来的正是 D1:1 与 D2:1 两轮原文，却因为那条路径
    不带 id 而被判为 0.0。假红与假绿一样有害——它会让人以为检索坏了，
    进而去"修"一个没坏的东西。

    为什么用原文匹配不算放水：比对的是**灌进去的那一串精确原文**
    （仅压空白/大小写），这是身份判定，不是语义相似度给分。改写、翻译、
    摘要一律不匹配——实测抽取路径把中文原文改写成英文摘要，那条就只能
    靠元数据认，不会从原文匹配这里蹭到分。

    同一段原文对应多个证据 id（重复表述）时按集合全记，并在判据里标明，
    不做任意取一。
    """
    if not evidence:
        return [], {}
    wanted = set(evidence)
    hits: dict[str, str] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata")
        if isinstance(meta, dict):
            ev = str(meta.get(meta_key) or "")
            if ev in wanted:
                hits[ev] = "metadata"
        # verbatim 路径：正文可能在 memory 或 content 里
        for field in ("memory", "content"):
            raw = item.get(field)
            if not isinstance(raw, str) or not raw:
                continue
            for ev in text_to_id.get(_norm_text(raw), ()):  # 精确原文回指
                if ev in wanted:
                    hits.setdefault(ev, "verbatim_text")
    return sorted(hits), {k: hits[k] for k in sorted(hits)}


def _parse_lme_date(text: str):
    import re
    from datetime import datetime

    cleaned = re.sub(r"\s*\([^)]*\)\s*", " ", str(text)).strip()
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析日期: {text!r}")


# ── LongMemEval 单实例执行 ──────────────────────────────────────────

def run_longmemeval_instance(
    adapter: AiduMEIBenchmarkAdapter,
    inst: dict,
    *,
    top_k: int,
    deterministic: bool = False,
) -> dict:
    """一个实例的完整生命周期：reset → 灌注 → 检索 → 诊断评分 → 清理。

    防泄漏铁律：只灌注日期 ≤ question_date 的会话（schema 校验器已把
    「问题日期之后的会话」计数上报，这里是强制排除的执行点）。

    ``deterministic``（v20，仅 G3b 自检用）：True 时以 ``infer=False``
    灌注 —— 服务端跳过 LLM 抽取、原文规范化直写，写入链路才可能
    「两遍 bit 相同」。**正式跑分一律 False**，与生产完全同路。
    """
    qid = str(inst["question_id"])
    adapter.reset_case("longmemeval", qid)
    q_date = _parse_lme_date(inst["question_date"])

    injected_sessions: list[str] = []
    excluded_sessions: list[str] = []
    # 原文 → 会话 id：verbatim 召回路径不带元数据，只能靠原文回指。
    # 理由与 _match_evidence 的文档一致。
    text_to_session: dict[str, set[str]] = {}
    for sid, sdate, session in zip(
        inst["haystack_session_ids"], inst["haystack_dates"],
        inst["haystack_sessions"],
    ):
        if _parse_lme_date(sdate) > q_date:
            excluded_sessions.append(str(sid))
            continue
        injected_sessions.append(str(sid))
        for turn_index, turn in enumerate(session):
            text_to_session.setdefault(
                _norm_text(str(turn["content"])), set()).add(str(sid))
            adapter.add_turn(
                case_id=qid,
                session_id=str(sid),
                turn_index=turn_index,
                role=str(turn["role"]),
                content=str(turn["content"]),
                timestamp=str(sdate),
                infer=not deterministic,
            )

    search_out = adapter.search(qid, str(inst["question"]), top_k=top_k)
    results = search_out["results"]

    # 诊断评分：检索结果里是否出现证据会话的标记（bench_session_id 随
    # metadata 写入）。这是检索链路诊断，不是官方 QA 指标。
    evidence_ids = {str(s) for s in inst["answer_session_ids"]}
    hit_list, hit_basis = _match_evidence(
        results, sorted(evidence_ids), text_to_session,
        meta_key="bench_session_id",
    )
    hit_ids = set(hit_list)

    retrieved_any = bool(results)
    record = {
        "question_id": qid,
        "question_type": str(inst["question_type"]),
        "abstention_case": qid.endswith("_abs"),
        "injected_sessions": injected_sessions,
        "excluded_sessions_after_question_date": excluded_sessions,
        "top_k": top_k,
        "retrieved_count": len(results),
        "evidence_session_ids": sorted(evidence_ids),
        "evidence_sessions_hit": sorted(hit_ids),
        "evidence_hit_basis": hit_basis,
        # v20 诚实性修正：没有证据会话的题（abstention 类）**不适用**召回
        # 诊断。此前记 0.0，会和「有证据但一条都没召回」在同一个数上撞车，
        # 拉低平均分 —— 用弃答题惩罚检索能力是把不适用当失败。N/A 用
        # null 表达，并配一个机器可读的 applicable 布尔量。
        "evidence_recall_applicable": bool(evidence_ids),
        "evidence_recall_diagnostic": (
            len(hit_ids) / len(evidence_ids) if evidence_ids else None
        ),
        # 诚实边界：没有检索到任何证据时不得作答
        "would_answer": retrieved_any,
        "retrieved_evidence_only": [
            item for item in results if isinstance(item, dict)
        ],
        "request_id": search_out["request_id"],
    }
    adapter.close_case(qid)
    return record


# ── LoCoMo 单样本执行 ───────────────────────────────────────────────

def run_locomo_sample(
    adapter: AiduMEIBenchmarkAdapter,
    sample: dict,
    *,
    top_k: int,
    max_qa: int | None = None,
    deterministic: bool = False,
) -> list[dict]:
    """一个 LoCoMo 样本：灌注全部会话（对话即历史，无未来泄漏问题——
    问题针对整段既有对话提问），逐题检索并出诊断记录。

    ``deterministic``：见 ``run_longmemeval_instance``（G3b 自检专用）。
    """
    import re

    sample_id = str(sample.get("sample_id") or sample.get("id") or "sample")
    adapter.reset_case("locomo", sample_id)
    conv = sample["conversation"]

    session_keys = sorted(
        (k for k in conv if re.fullmatch(r"session_\d+", k)),
        key=lambda k: int(k.split("_")[1]),
    )
    # 原文 → 该原文对应的证据 id 集合。见下方匹配器注释：verbatim 召回
    # 路径不带元数据，只能靠**灌进去的那串原文本身**回指是哪一轮。
    text_to_dia: dict[str, set[str]] = {}

    for key in session_keys:
        stamp = str(conv.get(f"{key}_date_time", ""))
        for turn_index, turn in enumerate(conv[key]):
            dia_id = str(turn.get("dia_id", ""))
            if dia_id:
                text_to_dia.setdefault(_norm_text(str(turn["text"])), set()).add(dia_id)
            adapter.add_turn(
                case_id=sample_id,
                session_id=key,
                turn_index=turn_index,
                role=str(turn["speaker"]),
                content=str(turn["text"]),
                timestamp=stamp,
                # v20 修：dia_id（形如 D1:1）是 LoCoMo 的**证据标识**，
                # 下面的匹配器就是拿它去召回结果里找。此前从未传入，
                # 于是 evidence_hits 结构性恒空、召回诊断恒 0.0。
                dia_id=dia_id,
                infer=not deterministic,
            )

    records: list[dict] = []
    qa_list = sample["qa"][:max_qa] if max_qa else sample["qa"]
    for j, qa in enumerate(qa_list):
        search_out = adapter.search(sample_id, str(qa["question"]), top_k=top_k)
        results = search_out["results"]
        evidence = [str(e) for e in (qa.get("evidence") or [])]
        hits, basis = _match_evidence(results, evidence, text_to_dia)
        records.append({
            "sample_id": sample_id,
            "qa_index": j,
            "category": int(qa["category"]),
            "top_k": top_k,
            "retrieved_count": len(results),
            "evidence_dia_ids": evidence,
            "evidence_hits": hits,
            # 每条命中是靠哪种信号认出来的（metadata / verbatim_text）：
            # 没有这一栏，「召回 1.0」就说不清是精确回指还是兜底匹配。
            "evidence_hit_basis": basis,
            # 同 longmemeval：无证据（category 5 对抗题）＝ 不适用，记 N/A
            "evidence_recall_applicable": bool(evidence),
            "evidence_recall_diagnostic": (
                len(hits) / len(evidence) if evidence else None
            ),
            "would_answer": bool(results),
            # v20 补：LoCoMo 记录此前**不含检索结果**，而 PROTOCOL.md §5
            # 承诺 JSONL 要留「检索结果」证据链。这既是协议欠账，也让
            # LoCoMo 的 digest 根本不依赖召回内容——正是它在管线断裂时
            # 仍两遍相等（假绿）的原因。与 longmemeval 记录对齐后，
            # digest 才真的对检索行为敏感。
            "retrieved_evidence_only": [
                item for item in results if isinstance(item, dict)
            ],
            "request_id": search_out["request_id"],
        })
    adapter.close_case(sample_id)
    return records


# ── smoke / formal 编排 ────────────────────────────────────────────

def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _recall_aggregate(records: list[dict]) -> dict:
    """汇总召回诊断：**跳过 N/A，不拿 0.0 充数**。

    弃答题（LongMemEval 的 ``_abs``、LoCoMo 的 category 5 对抗题）本就
    没有证据可召回，把它们当 0 分平均进去，得到的是一个既低估检索能力
    又无法解释的数。这里显式区分三种量：适用题数、N/A 题数、以及只在
    适用题上取的均值（无适用题时为 None，而不是 0.0）。
    """
    applicable = [r for r in records
                  if r.get("evidence_recall_diagnostic") is not None]
    n_a = len(records) - len(applicable)
    mean = (sum(float(r["evidence_recall_diagnostic"]) for r in applicable)
            / len(applicable)) if applicable else None
    return {
        "applicable_records": len(applicable),
        "not_applicable_records": n_a,
        "mean_over_applicable_only": mean,
        "note": "N/A（无证据的弃答/对抗题）不计入均值，也不记 0.0。",
    }


def run_smoke(base_url: str, dataset: str, *, top_k: int, out_dir: str,
              deterministic: bool = False) -> dict:
    """合成 fixture 上的端到端自检。

    两档闸门（PROTOCOL.md §5，v20 修订）：

    - 默认（``deterministic=False``，LLM 在环）＝ **G3a**：只断言结构不变量
      —— 记录条数、各失败分类计数、data_report 一致。digest 会变，因为
      LLM 抽取的正文每遍都可能不同，这是被测系统的性质，不是 bug。
    - ``--deterministic``（``infer=False``，LLM 出环）＝ **G3b**：写入通路
      变成纯规则/嵌入检索，此时 digest **必须 bit 相同**。
    """
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    adapter = AiduMEIBenchmarkAdapter(base_url)
    adapter.health()

    if dataset == "longmemeval":
        fixture = os.path.join(FIXTURES_DIR, "smoke_longmemeval.json")
        instances = _load_json(fixture)
        data_report = validate_longmemeval(instances)
        records = [
            run_longmemeval_instance(adapter, inst, top_k=top_k,
                                     deterministic=deterministic)
            for inst in instances
        ]
    elif dataset == "locomo":
        fixture = os.path.join(FIXTURES_DIR, "smoke_locomo.json")
        samples = _load_json(fixture)
        data_report = validate_locomo(samples)
        records = []
        for sample in samples:
            records.extend(run_locomo_sample(
                adapter, sample, top_k=top_k, deterministic=deterministic))
    else:
        raise ValueError(f"未知数据集: {dataset}")

    config = {
        "mode": "smoke",
        "dataset": dataset,
        "top_k": top_k,
        "fixture": os.path.basename(fixture),
        "fixture_sha256": _sha256_file(fixture),
        # 进 digest 的 config 字段：两种模式的 digest 因此永不可能相撞，
        # 不会出现「拿 G3b 的哈希去冒充 G3a 通过」。
        "write_path": "deterministic_infer_false" if deterministic else "production_infer_true",
        "gate": "G3b" if deterministic else "G3a",
        "data_report": data_report,
    }
    digest = _stable_digest(records, config)
    summary = {
        "config": config,
        "digest": digest,
        "records_total": len(records),
        "recall_aggregate": _recall_aggregate(records),
        "adapter_stats": dict(adapter.stats),
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": (
            "smoke 的 evidence_recall_diagnostic 是检索链路诊断，"
            "不是 LongMemEval/LoCoMo 官方指标，不得作为成绩宣称。"
            + ("　本次为 G3b 复现性自检（infer=false，LLM 出环），"
               "其召回数字更不得外传：免抽取直写与生产语义不同。"
               if deterministic else "")
        ),
    }

    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    suffix = "_det" if deterministic else ""
    jsonl_path = os.path.join(out_dir, f"smoke_{dataset}{suffix}_{stamp}.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    summary["jsonl"] = jsonl_path
    with open(os.path.join(out_dir, f"smoke_{dataset}{suffix}_{stamp}_summary.json"),
              "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, sort_keys=True)
    return summary


def _check_formal_manifest(dataset: str, data_path: str) -> dict:
    """formal 闸门：manifest 必须存在、无 PENDING、且数据哈希对得上。"""
    if not os.path.exists(MANIFEST_PATH):
        raise SystemExit(
            "formal 拒绝启动：缺 data_manifest.json（先跑 download.py 锁哈希）"
        )
    manifest = _load_json(MANIFEST_PATH)
    entry = manifest.get(dataset) or {}
    locked = entry.get("sha256", "")
    if not locked or "PENDING" in str(locked).upper():
        raise SystemExit(
            f"formal 拒绝启动：{dataset} 的哈希仍是 PENDING——协议未锁定"
        )
    actual = _sha256_file(data_path)
    if actual != locked:
        raise SystemExit(
            f"formal 拒绝启动：{dataset} 数据哈希不匹配\n"
            f"  manifest: {locked}\n  实际:     {actual}"
        )
    return entry


def run_formal(base_url: str, dataset: str, *, top_k: int, data_path: str,
               out_dir: str) -> dict:
    """正式运行：哈希闸门 → 严格 schema（500/10）→ 全量执行 → 留证。"""
    entry = _check_formal_manifest(dataset, data_path)
    instances = _load_json(data_path)
    if dataset == "longmemeval":
        data_report = validate_longmemeval(instances, expect_total=500)
        adapter = AiduMEIBenchmarkAdapter(base_url)
        adapter.health()
        records = [
            run_longmemeval_instance(adapter, inst, top_k=top_k)
            for inst in instances
        ]
    elif dataset == "locomo":
        data_report = validate_locomo(instances, expect_samples=10)
        adapter = AiduMEIBenchmarkAdapter(base_url)
        adapter.health()
        records = []
        for sample in instances:
            records.extend(run_locomo_sample(adapter, sample, top_k=top_k))
    else:
        raise ValueError(f"未知数据集: {dataset}")

    config = {
        "mode": "formal",
        "dataset": dataset,
        "top_k": top_k,
        "data_path": os.path.basename(data_path),
        "data_sha256": entry["sha256"],
        "manifest_entry": entry,
        # formal 没有 deterministic 开关：正式成绩必须与生产同路
        # （infer=true，LLM 抽取在环）。这里写死，不接受参数。
        "write_path": "production_infer_true",
        "data_report": data_report,
    }
    digest = _stable_digest(records, config)
    summary = {
        "config": config,
        "digest": digest,
        "records_total": len(records),
        "recall_aggregate": _recall_aggregate(records),
        "adapter_stats": dict(adapter.stats),
        "note": (
            "本 JSONL 只含检索证据链与诊断召回；官方 QA 指标须走官方 "
            "evaluator（LongMemEval evaluate_qa.py / LoCoMo 官方评分），"
            "judge 模型与温度见 PROTOCOL.md。"
        ),
    }
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    jsonl_path = os.path.join(out_dir, f"formal_{dataset}_{stamp}.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    summary["jsonl"] = jsonl_path
    with open(os.path.join(out_dir, f"formal_{dataset}_{stamp}_summary.json"),
              "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, sort_keys=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="benchmarks.run",
        description="aiduMEI 评测管线（smoke 自检 / formal 正式运行）",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--smoke", action="store_true", help="合成 fixture 自检")
    mode.add_argument("--formal", action="store_true", help="正式运行（需哈希闸门）")
    parser.add_argument("--dataset", required=True,
                        choices=("longmemeval", "locomo"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--data-path", help="formal 模式的数据文件路径")
    parser.add_argument("--out-dir", default=os.path.join("benchmarks", "runs"))
    parser.add_argument(
        "--deterministic", action="store_true",
        help=("仅 smoke：以 infer=false 灌注（LLM 出环），用于 G3b "
              "bit 级复现性自检。此模式的召回数字不代表生产表现，"
              "更不得作为成绩；formal 拒绝此开关。"),
    )
    args = parser.parse_args(argv)

    try:
        if args.smoke:
            summary = run_smoke(args.base_url, args.dataset,
                                top_k=args.top_k, out_dir=args.out_dir,
                                deterministic=args.deterministic)
        else:
            if not args.data_path:
                parser.error("--formal 需要 --data-path")
            if args.deterministic:
                # 正式成绩绝不许走免抽取通路：那是另一个系统的成绩。
                parser.error("--formal 不接受 --deterministic（正式跑分必须与生产同路）")
            summary = run_formal(args.base_url, args.dataset,
                                 top_k=args.top_k, data_path=args.data_path,
                                 out_dir=args.out_dir)
    except AdapterError as e:
        print(f"运行失败（{e.kind}）: {e}", file=sys.stderr)
        return 2

    print(json.dumps(
        {"gate": summary["config"].get("gate", "formal"),
         "write_path": summary["config"]["write_path"],
         "digest": summary["digest"],
         "records_total": summary["records_total"],
         "recall_aggregate": summary["recall_aggregate"],
         "adapter_stats": summary["adapter_stats"],
         "jsonl": summary["jsonl"]},
        ensure_ascii=False, indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
