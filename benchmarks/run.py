"""benchmarks.run — 评测编排：smoke（可重复自检）与 formal（正式运行）。

诚实边界（v20.0 权威方案 §4.4）：

- smoke 用**合成 fixture**（官方 schema 形状、安全假内容）验证管线端到端
  可用与可重复：同一 fixture 跑两遍 digest 必须一致。smoke 的诊断分只衡量
  「检索到证据会话」的召回诊断，**不是** LongMemEval/LoCoMo 官方指标，
  不得当成绩宣传。
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
    """对确定性内容取哈希：剔除时间戳/延迟/request_id 类波动字段。"""
    volatile = {"request_id", "request_ids", "latency_ms", "started_at",
                "finished_at", "duration_s", "raw"}

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
) -> dict:
    """一个实例的完整生命周期：reset → 灌注 → 检索 → 诊断评分 → 清理。

    防泄漏铁律：只灌注日期 ≤ question_date 的会话（schema 校验器已把
    「问题日期之后的会话」计数上报，这里是强制排除的执行点）。
    """
    qid = str(inst["question_id"])
    adapter.reset_case("longmemeval", qid)
    q_date = _parse_lme_date(inst["question_date"])

    injected_sessions: list[str] = []
    excluded_sessions: list[str] = []
    for sid, sdate, session in zip(
        inst["haystack_session_ids"], inst["haystack_dates"],
        inst["haystack_sessions"],
    ):
        if _parse_lme_date(sdate) > q_date:
            excluded_sessions.append(str(sid))
            continue
        injected_sessions.append(str(sid))
        for turn_index, turn in enumerate(session):
            adapter.add_turn(
                case_id=qid,
                session_id=str(sid),
                turn_index=turn_index,
                role=str(turn["role"]),
                content=str(turn["content"]),
                timestamp=str(sdate),
            )

    search_out = adapter.search(qid, str(inst["question"]), top_k=top_k)
    results = search_out["results"]

    # 诊断评分：检索结果里是否出现证据会话的标记（bench_session_id 随
    # metadata 写入）。这是检索链路诊断，不是官方 QA 指标。
    evidence_ids = {str(s) for s in inst["answer_session_ids"]}
    hit_ids: set[str] = set()
    for item in results:
        meta = item.get("metadata") if isinstance(item, dict) else None
        blob = json.dumps(item, ensure_ascii=False)
        for ev in evidence_ids:
            if (isinstance(meta, dict) and str(meta.get("bench_session_id")) == ev) \
                    or f'"{ev}"' in blob:
                hit_ids.add(ev)

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
        "evidence_recall_diagnostic": (
            len(hit_ids) / len(evidence_ids) if evidence_ids else 0.0
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
) -> list[dict]:
    """一个 LoCoMo 样本：灌注全部会话（对话即历史，无未来泄漏问题——
    问题针对整段既有对话提问），逐题检索并出诊断记录。"""
    import re

    sample_id = str(sample.get("sample_id") or sample.get("id") or "sample")
    adapter.reset_case("locomo", sample_id)
    conv = sample["conversation"]

    session_keys = sorted(
        (k for k in conv if re.fullmatch(r"session_\d+", k)),
        key=lambda k: int(k.split("_")[1]),
    )
    for key in session_keys:
        stamp = str(conv.get(f"{key}_date_time", ""))
        for turn_index, turn in enumerate(conv[key]):
            adapter.add_turn(
                case_id=sample_id,
                session_id=key,
                turn_index=turn_index,
                role=str(turn["speaker"]),
                content=str(turn["text"]),
                timestamp=stamp,
            )

    records: list[dict] = []
    qa_list = sample["qa"][:max_qa] if max_qa else sample["qa"]
    for j, qa in enumerate(qa_list):
        search_out = adapter.search(sample_id, str(qa["question"]), top_k=top_k)
        results = search_out["results"]
        evidence = [str(e) for e in (qa.get("evidence") or [])]
        blob = json.dumps(results, ensure_ascii=False)
        hits = [ev for ev in evidence if f'"{ev}"' in blob]
        records.append({
            "sample_id": sample_id,
            "qa_index": j,
            "category": int(qa["category"]),
            "top_k": top_k,
            "retrieved_count": len(results),
            "evidence_dia_ids": evidence,
            "evidence_hits": hits,
            "evidence_recall_diagnostic": (
                len(hits) / len(evidence) if evidence else 0.0
            ),
            "would_answer": bool(results),
            "request_id": search_out["request_id"],
        })
    adapter.close_case(sample_id)
    return records


# ── smoke / formal 编排 ────────────────────────────────────────────

def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_smoke(base_url: str, dataset: str, *, top_k: int, out_dir: str) -> dict:
    """合成 fixture 上的端到端自检。两次运行 digest 必须一致（G3）。"""
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    adapter = AiduMEIBenchmarkAdapter(base_url)
    adapter.health()

    if dataset == "longmemeval":
        fixture = os.path.join(FIXTURES_DIR, "smoke_longmemeval.json")
        instances = _load_json(fixture)
        data_report = validate_longmemeval(instances)
        records = [
            run_longmemeval_instance(adapter, inst, top_k=top_k)
            for inst in instances
        ]
    elif dataset == "locomo":
        fixture = os.path.join(FIXTURES_DIR, "smoke_locomo.json")
        samples = _load_json(fixture)
        data_report = validate_locomo(samples)
        records = []
        for sample in samples:
            records.extend(run_locomo_sample(adapter, sample, top_k=top_k))
    else:
        raise ValueError(f"未知数据集: {dataset}")

    config = {
        "mode": "smoke",
        "dataset": dataset,
        "top_k": top_k,
        "fixture": os.path.basename(fixture),
        "fixture_sha256": _sha256_file(fixture),
        "data_report": data_report,
    }
    digest = _stable_digest(records, config)
    summary = {
        "config": config,
        "digest": digest,
        "records_total": len(records),
        "adapter_stats": dict(adapter.stats),
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": (
            "smoke 的 evidence_recall_diagnostic 是检索链路诊断，"
            "不是 LongMemEval/LoCoMo 官方指标，不得作为成绩宣称。"
        ),
    }

    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    jsonl_path = os.path.join(out_dir, f"smoke_{dataset}_{stamp}.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    summary["jsonl"] = jsonl_path
    with open(os.path.join(out_dir, f"smoke_{dataset}_{stamp}_summary.json"),
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
        "data_report": data_report,
    }
    digest = _stable_digest(records, config)
    summary = {
        "config": config,
        "digest": digest,
        "records_total": len(records),
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
    args = parser.parse_args(argv)

    try:
        if args.smoke:
            summary = run_smoke(args.base_url, args.dataset,
                                top_k=args.top_k, out_dir=args.out_dir)
        else:
            if not args.data_path:
                parser.error("--formal 需要 --data-path")
            summary = run_formal(args.base_url, args.dataset,
                                 top_k=args.top_k, data_path=args.data_path,
                                 out_dir=args.out_dir)
    except AdapterError as e:
        print(f"运行失败（{e.kind}）: {e}", file=sys.stderr)
        return 2

    print(json.dumps(
        {"digest": summary["digest"],
         "records_total": summary["records_total"],
         "adapter_stats": summary["adapter_stats"],
         "jsonl": summary["jsonl"]},
        ensure_ascii=False, indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
