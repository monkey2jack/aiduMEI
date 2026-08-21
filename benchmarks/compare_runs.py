"""benchmarks.compare_runs — G3 复现性闸门的执行器（v20.0）。

PROTOCOL.md §5 规定了 G3a/G3b 两档断言，但此前「跑两遍比一比」全靠人手
眼看：眼看会漏、会自我说服，也没法进 CI。本模块把那两条断言写成可执行、
可失败、退出码可判的检查。

两档断言（与 PROTOCOL.md §5 表格逐条对应）：

* ``--gate g3a``：生产同路（``infer=true``）。只断言**结构不变量**——
  记录数、``data_report``、全部失败分类计数、每条记录的
  ``retrieved_count>0`` 与 ``would_answer``、证据命中集合。digest **允许
  不同**（远程 LLM 抽取的正文不保证逐字节复现）。
* ``--gate g3b``：``--deterministic``（``infer=false``，LLM 出环）。除上述
  结构不变量外，断言 **digest bit 相同**；模型派生浮点（``score`` /
  ``_hybrid_score``）另按显式容差检查，并报出**实测噪声地板**。

反假绿（本模块存在的主要理由之一）：两个都空的运行当然处处相等。
历史上 locomo 的 digest 两遍相等就是这么来的——3 条记录里 2 条为空、
run B 吞掉 3 次超时 + 3 次重试却没扰动 digest。因此任何「通过」都必须
先过实质性检查：记录数 > 0、**并非全部** ``retrieved_count = 0``、且
**至少有一条适用题产生了非空证据命中**；超时等失败分类一个都不许漂
（当年那 3 次超时正是被这条抓住的）。部分空结果只警告不判失败——空
结果是协议承认的合法结果，拒答题的空更是正确行为，理由见
``check_substantive`` 的文档。实质性不达标时本模块报 FAIL，绝不报绿。

用法::

    python -m benchmarks.compare_runs --gate g3b A_summary.json B_summary.json

只用标准库。退出码：0 通过，1 断言失败，2 用法/文件问题。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# 与 run.py::_stable_digest 的剔除集合保持一致的「模型派生浮点」
TOLERANCE_FIELDS = ("score", "_hybrid_score")
# 默认容差：实测远程 embedding 抖动 |Δ|≈6e-4（G3b，LLM 已出环），
# 取一个数量级余量。调大必须在 PROTOCOL.md 留痕并说明理由。
DEFAULT_TOLERANCE = 5e-3


class GateFailure(AssertionError):
    """闸门断言失败——带上人能直接读懂的定位信息。"""


def _load(path: str) -> tuple[dict, list[dict]]:
    with open(path, encoding="utf-8") as f:
        summary = json.load(f)
    jsonl_path = summary.get("jsonl")
    if not jsonl_path:
        raise GateFailure(f"{path} 里没有 jsonl 指针，无法核对原始记录")
    with open(jsonl_path, encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    return summary, records


def _evidence_pair(rec: dict) -> tuple[list, list]:
    """取出（期望证据, 命中证据），两个数据集的字段名不同。

    两边的证据**粒度本来就不同**，不该强行统一命名：LongMemEval 的证据
    是会话级（``evidence_session_ids`` / ``evidence_sessions_hit``），
    LoCoMo 的是对话轮级（``evidence_dia_ids`` / ``evidence_hits``）。
    所以这里做的是识别，不是改写记录 —— 少认一种就会把「命中」误判成
    「全空」，那是假红；而假红和假绿一样会让闸门失去意义。
    """
    for want_key, hit_key in (("evidence_dia_ids", "evidence_hits"),
                              ("evidence_session_ids", "evidence_sessions_hit")):
        if want_key in rec or hit_key in rec:
            return list(rec.get(want_key) or []), list(rec.get(hit_key) or [])
    return [], []


def _record_key(rec: dict, index: int) -> str:
    """记录的语义标识：优先用题号，缺了才退化到序号。"""
    for key in ("question_id", "sample_id", "case_id"):
        val = rec.get(key)
        if val:
            return f"{key}={val}"
    return f"#{index}"


def check_substantive(records: list[dict], label: str) -> tuple[list[str], list[str]]:
    """反假绿：空跑不许算通过。返回 (硬失败清单, 警告清单)。

    哪些算硬失败、哪些只算警告，这条线划在**「闸门在管什么」**上：
    G3 管的是复现性（两遍一不一样），不是召回率。所以

    * **全空**才判失败——一个什么都没检索到的运行，处处相等是必然的，
      这种「一致」不含任何信息量（历史上 locomo 的假绿正是这一类）。
    * **部分空**只警告。空结果是协议明文承认的合法结果（§3.3），而且
      LoCoMo cat5 是**拒答题**，`retrieved_count=0` 恰恰是它的正确行为
      （§4）——拿它当失败，等于让闸门因为数据本身的难度长红。一条会因
      正当原因长红的闸门等于没有闸门，这与拆分 G3a/G3b 的理由是同一条。

    警告不是"降级放过"：它带着实质记录占比一起进报告和 stderr，弱证明力
    是**写明的**，而不是被吞掉的。判召回好坏是 `recall_aggregate` 的活。
    """
    problems: list[str] = []
    warnings: list[str] = []
    if not records:
        problems.append(f"{label}: 记录数为 0——空跑不算通过")
        return problems, warnings

    empty = [_record_key(r, i) for i, r in enumerate(records)
             if not int(r.get("retrieved_count") or 0)]
    if len(empty) == len(records):
        problems.append(
            f"{label}: 全部 {len(records)} 条记录 retrieved_count=0——"
            "检索什么都没返回时的「一致」是假绿，不算通过"
        )
    elif empty:
        abstain = sum(1 for r in records
                      if not int(r.get("retrieved_count") or 0)
                      and not r.get("evidence_recall_applicable"))
        warnings.append(
            f"{label}: {len(records) - len(empty)}/{len(records)} 条记录有实质检索结果"
            f"；空结果 {len(empty)} 条（{', '.join(empty[:5])}"
            f"{'…' if len(empty) > 5 else ''}），其中 {abstain} 条是无证据的拒答题"
            "（合法）。空的那些对本闸门不提供证明力。"
        )

    applicable = [r for r in records if r.get("evidence_recall_applicable")]
    if applicable and not any(_evidence_pair(r)[1] for r in applicable):
        problems.append(
            f"{label}: {len(applicable)} 条适用题的 evidence_hits 全为空——"
            "证据链结构性断裂（这正是 v20 修掉的那个 dia_id bug 的症状）"
        )
    return problems, warnings


def _iter_scores(records: list[dict]):
    """逐个产出 (定位路径, 字段名, 数值)，顺序稳定以便两遍对齐。"""
    for i, rec in enumerate(records):
        items = rec.get("retrieved_evidence_only") or []
        for j, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            for field in TOLERANCE_FIELDS:
                val = item.get(field)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    yield f"{_record_key(rec, i)}.retrieved[{j}].{field}", field, float(val)


def check_score_tolerance(rec_a: list[dict], rec_b: list[dict],
                          tolerance: float) -> tuple[list[str], float, int]:
    """模型派生浮点的容差检查。返回 (问题清单, 实测最大 |Δ|, 比较对数)。"""
    a_list = list(_iter_scores(rec_a))
    b_list = list(_iter_scores(rec_b))
    problems: list[str] = []
    if len(a_list) != len(b_list):
        problems.append(
            f"可比数值个数不同：A={len(a_list)} B={len(b_list)}——"
            "结构已经变了，不是数值抖动问题"
        )
        return problems, float("nan"), 0

    worst = 0.0
    worst_where: str | None = None
    for (path_a, _f, va), (path_b, _g, vb) in zip(a_list, b_list):
        if path_a != path_b:
            problems.append(f"数值位置对不上：A 的 {path_a} vs B 的 {path_b}")
            continue
        delta = abs(va - vb)
        # `worst_where is None` 这一支不可省：两遍完全一致时 delta 恒为 0.0，
        # 永远不会 `> worst`，噪声地板会被整条吞掉——而 |Δ|=0 恰恰是最强的
        # 结果，最该报出来。闸门必须报出它**量到了什么**，不能只在有抖动时开口。
        if worst_where is None or delta > worst:
            worst, worst_where = delta, path_a
        if delta > tolerance:
            problems.append(
                f"{path_a}: |Δ|={delta:.3e} 超出容差 {tolerance:.1e}"
                f"（A={va!r} B={vb!r}）"
            )
    if worst_where is not None:
        problems.append(f"·实测噪声地板：最大 |Δ|={worst:.3e} @ {worst_where}"
                        f"（{len(a_list)} 个数值，容差 {tolerance:.1e}）")
    return problems, worst, len(a_list)


def check_structural(sum_a: dict, sum_b: dict,
                     rec_a: list[dict], rec_b: list[dict]) -> list[str]:
    """结构不变量：G3a 与 G3b 共同的底线。"""
    problems: list[str] = []

    if sum_a.get("records_total") != sum_b.get("records_total"):
        problems.append(
            f"records_total 不同：{sum_a.get('records_total')} vs "
            f"{sum_b.get('records_total')}"
        )
    if len(rec_a) != len(rec_b):
        problems.append(f"JSONL 行数不同：{len(rec_a)} vs {len(rec_b)}")

    for key in ("dataset", "mode", "top_k", "gate", "write_path",
                "fixture_sha256", "data_sha256"):
        va = (sum_a.get("config") or {}).get(key)
        vb = (sum_b.get("config") or {}).get(key)
        if va != vb:
            problems.append(f"config.{key} 不同：{va!r} vs {vb!r}——两遍配置就不一样，无从比较")

    da = (sum_a.get("config") or {}).get("data_report")
    db = (sum_b.get("config") or {}).get("data_report")
    if da != db:
        problems.append("data_report 不同——装载器对同一份数据的读数变了")

    stats_a = sum_a.get("adapter_stats") or {}
    stats_b = sum_b.get("adapter_stats") or {}
    # requests/retries 允许不同（重试是环境噪声），失败分类一个都不许漂
    for key in sorted(set(stats_a) | set(stats_b)):
        if key in ("requests", "retries"):
            continue
        if stats_a.get(key) != stats_b.get(key):
            problems.append(
                f"adapter_stats.{key} 漂移：{stats_a.get(key)} vs {stats_b.get(key)}"
            )

    for i, (ra, rb) in enumerate(zip(rec_a, rec_b)):
        where = _record_key(ra, i)
        if _record_key(rb, i) != where:
            problems.append(f"记录 #{i} 身份对不上：{where} vs {_record_key(rb, i)}")
            continue
        for key in ("would_answer", "evidence_recall_applicable"):
            if ra.get(key) != rb.get(key):
                problems.append(f"{where}.{key} 不同：{ra.get(key)} vs {rb.get(key)}")
        if bool(ra.get("retrieved_count")) != bool(rb.get("retrieved_count")):
            problems.append(
                f"{where}.retrieved_count 一遍有一遍无："
                f"{ra.get('retrieved_count')} vs {rb.get('retrieved_count')}"
            )
        want_a, hits_a = _evidence_pair(ra)
        want_b, hits_b = _evidence_pair(rb)
        if sorted(map(str, want_a)) != sorted(map(str, want_b)):
            problems.append(f"{where} 期望证据不同：{want_a} vs {want_b}")
        if sorted(map(str, hits_a)) != sorted(map(str, hits_b)):
            problems.append(f"{where} 证据命中不同：{hits_a} vs {hits_b}")
        if ra.get("evidence_recall_diagnostic") != rb.get("evidence_recall_diagnostic"):
            problems.append(
                f"{where}.evidence_recall_diagnostic 不同："
                f"{ra.get('evidence_recall_diagnostic')} vs "
                f"{rb.get('evidence_recall_diagnostic')}"
            )
    return problems


def compare(path_a: str, path_b: str, gate: str,
            tolerance: float = DEFAULT_TOLERANCE) -> dict:
    """执行一档闸门。返回报告 dict；``passed`` 为最终结论。"""
    gate = gate.lower()
    if gate not in ("g3a", "g3b"):
        raise GateFailure(f"未知闸门 {gate!r}（只有 g3a / g3b）")

    sum_a, rec_a = _load(path_a)
    sum_b, rec_b = _load(path_b)

    expected_gate = "G3a" if gate == "g3a" else "G3b"
    # 大小写不敏感比较：闸门名的大小写不是语义（run.py 写的是 "G3b"）
    declared = {str((sum_a.get("config") or {}).get("gate") or "").lower(),
                str((sum_b.get("config") or {}).get("gate") or "").lower()}
    gate_mismatch: list[str] = []
    if declared != {gate}:
        # 拿 G3b 的哈希冒充 G3a 通过，或反之——直接拦住
        gate_mismatch.append(
            f"运行自称的闸门 {sorted(x for x in declared if x)} 与 --gate "
            f"{expected_gate} 不符：不同写入通路的结果不可互相顶替"
        )

    sub_a, warn_a = check_substantive(rec_a, "A")
    sub_b, warn_b = check_substantive(rec_b, "B")
    substantive = sub_a + sub_b
    warnings = warn_a + warn_b
    structural = check_structural(sum_a, sum_b, rec_a, rec_b)

    digest_a, digest_b = sum_a.get("digest"), sum_b.get("digest")
    digest_equal = bool(digest_a) and digest_a == digest_b

    failures = list(gate_mismatch) + substantive + structural
    notes: list[str] = []
    worst = None
    compared = 0

    if gate == "g3b":
        tol_problems, worst, compared = check_score_tolerance(rec_a, rec_b, tolerance)
        # 以「·」开头的是信息行（噪声地板），不算失败
        failures += [p for p in tol_problems if not p.startswith("·")]
        notes += [p[1:] for p in tol_problems if p.startswith("·")]
        if not digest_equal:
            failures.append(
                f"G3b digest 不相同：{digest_a} vs {digest_b}"
                "（免抽取通路必须逐字节复现）"
            )
    else:
        notes.append(
            "G3a 不断言 digest 相同：远程 LLM 抽取的正文不保证逐字节复现"
            f"（本次 {'恰好相同' if digest_equal else '不同，符合预期'}）"
        )

    return {
        "gate": expected_gate,
        "passed": not failures,
        "records_compared": len(rec_a),
        "digest_a": digest_a,
        "digest_b": digest_b,
        "digest_equal": digest_equal,
        "score_values_compared": compared,
        "max_abs_delta": worst,
        "tolerance": tolerance if gate == "g3b" else None,
        "failures": failures,
        "warnings": warnings,
        "notes": notes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="G3 复现性闸门执行器（见 benchmarks/PROTOCOL.md §5）")
    parser.add_argument("summary_a", help="第一遍运行的 summary JSON")
    parser.add_argument("summary_b", help="第二遍运行的 summary JSON")
    parser.add_argument("--gate", required=True, choices=["g3a", "g3b"],
                        help="g3a=生产同路（只查结构不变量）；g3b=免抽取（还要 bit 相同）")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                        help=f"G3b 模型派生浮点的容差（默认 {DEFAULT_TOLERANCE:.0e}）")
    args = parser.parse_args(argv)

    try:
        report = compare(args.summary_a, args.summary_b, args.gate, args.tolerance)
    except GateFailure as e:
        print(f"[闸门无法执行] {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"[闸门无法执行] 读取失败：{e}", file=sys.stderr)
        return 2

    printable: dict[str, Any] = {k: v for k, v in report.items()}
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    # 警告也走 stderr：通过了但证明力弱，必须让人看见，不能只躺在 JSON 里
    for w in report.get("warnings") or []:
        print(f"[警告] {w}", file=sys.stderr)
    print(f"\n{report['gate']}: {'通过' if report['passed'] else '失败'}", file=sys.stderr)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
