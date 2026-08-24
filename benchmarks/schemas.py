"""benchmarks.schemas — 数据装载前的 schema validator（v20.0 §4.2）。

原则：**类别计数由装载器现场生成，不手抄**。校验器不修数据——上游原始
版保持原样，已知标注问题记进报告的 ``anomalies``，由版本化 correction
manifest 决定是否在评分时敏感性分析（绝不静默改数）。

LongMemEval（MIT，官方仓库 xiaowu0162/LongMemEval）：
  每个 S/M/oracle 数据文件 500 实例；六种 question_type，abstention
  以 question_id 的 ``_abs`` 后缀表达；oracle 文件只含证据会话，
  仅作检索上限诊断，不作 headline。

LoCoMo（CC BY-NC 4.0，官方仓库 snap-research/locomo）：
  10 段长对话；QA 类别 1 多跳、2 时间、3 开放域/常识、4 单跳、
  5 对抗/拒答（部分 cat5 只有 ``adversarial_answer``）；图片不随仓库
  发布（只有外部 URL/caption），本管线不抓取、不缓存图片。
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Any

# 官方六类题型（五项能力 + abstention 由 _abs 规则表达）
LONGMEMEVAL_QUESTION_TYPES = frozenset({
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "temporal-reasoning",
    "knowledge-update",
    "multi-session",
})

LOCOMO_CATEGORIES = frozenset({1, 2, 3, 4, 5})

_SESSION_KEY_RE = re.compile(r"^session_(\d+)$")


class SchemaError(ValueError):
    """结构性校验失败——带上出错实例的定位信息。"""


def _parse_lme_date(text: str) -> datetime:
    """LongMemEval 日期形如 '2023/05/20 (Sat) 02:21'。"""
    cleaned = re.sub(r"\s*\([^)]*\)\s*", " ", str(text)).strip()
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    raise SchemaError(f"无法解析 LongMemEval 日期: {text!r}")


def validate_longmemeval(
    instances: Any,
    *,
    expect_total: int | None = None,
) -> dict:
    """校验 LongMemEval 实例列表，返回现场生成的统计报告。

    ``expect_total``：正式文件传 500（官方每文件实例数）；smoke fixture
    传 None 跳过总数断言。结构性违规抛 SchemaError；数据内容层面的
    可疑点（如问题日期之后的会话）进 report，供 runner 强制排除。
    """
    if not isinstance(instances, list) or not instances:
        raise SchemaError("LongMemEval 数据必须是非空实例列表")
    if expect_total is not None and len(instances) != expect_total:
        raise SchemaError(
            f"实例数 {len(instances)} ≠ 期望 {expect_total}（官方每文件 500）"
        )

    required = (
        "question_id", "question_type", "question", "answer",
        "question_date", "haystack_dates", "haystack_session_ids",
        "haystack_sessions", "answer_session_ids",
    )
    seen_ids: set[str] = set()
    type_counts: Counter[str] = Counter()
    abstention = 0
    sessions_after_question = 0  # runner 必须排除，防时间泄漏
    intraday_unordered = 0       # 日内时刻颠倒：上游常态，只记不拦

    for i, inst in enumerate(instances):
        where = f"instance[{i}]"
        if not isinstance(inst, dict):
            raise SchemaError(f"{where} 不是对象")
        missing = [k for k in required if k not in inst]
        if missing:
            raise SchemaError(f"{where} 缺字段: {missing}")

        qid = str(inst["question_id"])
        if qid in seen_ids:
            raise SchemaError(f"{where} question_id 重复: {qid}")
        seen_ids.add(qid)

        qtype = str(inst["question_type"])
        if qtype not in LONGMEMEVAL_QUESTION_TYPES:
            raise SchemaError(f"{where} 未知题型: {qtype}")
        type_counts[qtype] += 1
        if qid.endswith("_abs"):
            abstention += 1

        ids = inst["haystack_session_ids"]
        dates = inst["haystack_dates"]
        sessions = inst["haystack_sessions"]
        if not (isinstance(ids, list) and isinstance(dates, list)
                and isinstance(sessions, list)):
            raise SchemaError(f"{where} haystack 三列必须都是列表")
        if not (len(ids) == len(dates) == len(sessions)):
            raise SchemaError(
                f"{where} haystack 长度不齐: ids={len(ids)} "
                f"dates={len(dates)} sessions={len(sessions)}"
            )

        parsed_dates = [_parse_lme_date(d) for d in dates]
        # 上游只保证 haystack 按【天】升序，不保证日内时刻有序。实测官方
        # longmemeval_s / longmemeval_oracle 各 500 实例：按天乱序 0/500，
        # 按完整时间戳乱序 211/500、34/500，且 **全部** 只是日内时刻颠倒
        # （211==211、34==34）。故结构断言下调到「天」，日内颠倒按本模块
        # 既定分工计入 report，不抛异常。
        parsed_days = [d.date() for d in parsed_dates]
        if parsed_days != sorted(parsed_days):
            raise SchemaError(f"{where} haystack_dates 未按日期升序")
        if parsed_dates != sorted(parsed_dates):
            intraday_unordered += 1
        q_date = _parse_lme_date(inst["question_date"])
        sessions_after_question += sum(1 for d in parsed_dates if d > q_date)

        for j, session in enumerate(sessions):
            if not isinstance(session, list):
                raise SchemaError(f"{where}.haystack_sessions[{j}] 不是回合列表")
            for k, turn in enumerate(session):
                if not isinstance(turn, dict) or "role" not in turn or "content" not in turn:
                    raise SchemaError(
                        f"{where}.haystack_sessions[{j}][{k}] 缺 role/content"
                    )

        evidence = inst["answer_session_ids"]
        if not isinstance(evidence, list) or not evidence:
            raise SchemaError(f"{where} answer_session_ids 为空——证据不可解析")
        unknown = set(map(str, evidence)) - set(map(str, ids))
        if unknown:
            raise SchemaError(f"{where} 证据会话不在 haystack 内: {sorted(unknown)}")

    return {
        "dataset": "longmemeval",
        "total": len(instances),
        "type_counts": dict(type_counts),
        "abstention": abstention,
        "sessions_after_question": sessions_after_question,
        "intraday_unordered": intraday_unordered,
    }


def validate_locomo(
    samples: Any,
    *,
    expect_samples: int | None = None,
) -> dict:
    """校验 LoCoMo 样本列表，返回现场生成的统计报告。

    结构性违规（重复 dia_id、未知类别、缺问题、会话缺时间戳）抛
    SchemaError；官方已知的数据陷阱（cat5 缺 answer、evidence 引用
    不存在的 dia_id、非 cat5 缺 answer）**如实进 anomalies**——上游
    原始版不改，评分阶段由 correction manifest 决定敏感性分析。
    """
    if not isinstance(samples, list) or not samples:
        raise SchemaError("LoCoMo 数据必须是非空样本列表")
    if expect_samples is not None and len(samples) != expect_samples:
        raise SchemaError(
            f"样本数 {len(samples)} ≠ 期望 {expect_samples}（官方发布 10 段）"
        )

    category_counts: Counter[int] = Counter()
    qa_total = 0
    anomalies: list[str] = []

    for i, sample in enumerate(samples):
        where = f"sample[{i}]"
        if not isinstance(sample, dict):
            raise SchemaError(f"{where} 不是对象")
        conv = sample.get("conversation")
        qa_list = sample.get("qa")
        if not isinstance(conv, dict) or not isinstance(qa_list, list):
            raise SchemaError(f"{where} 缺 conversation/qa")
        for who in ("speaker_a", "speaker_b"):
            if not conv.get(who):
                raise SchemaError(f"{where}.conversation 缺 {who}")

        dia_ids: set[str] = set()
        session_nums = []
        for key, value in conv.items():
            m = _SESSION_KEY_RE.fullmatch(key)
            if not m:
                continue
            session_nums.append(int(m.group(1)))
            if f"{key}_date_time" not in conv:
                raise SchemaError(f"{where}.conversation.{key} 缺 {key}_date_time")
            if not isinstance(value, list):
                raise SchemaError(f"{where}.conversation.{key} 不是回合列表")
            for k, turn in enumerate(value):
                if not isinstance(turn, dict):
                    raise SchemaError(f"{where}.{key}[{k}] 不是对象")
                for field in ("speaker", "dia_id", "text"):
                    if field not in turn:
                        raise SchemaError(f"{where}.{key}[{k}] 缺 {field}")
                did = str(turn["dia_id"])
                if did in dia_ids:
                    raise SchemaError(f"{where} dia_id 重复: {did}")
                dia_ids.add(did)
        if not session_nums:
            raise SchemaError(f"{where}.conversation 没有任何 session_N")

        for j, qa in enumerate(qa_list):
            qwhere = f"{where}.qa[{j}]"
            if not isinstance(qa, dict) or not qa.get("question"):
                raise SchemaError(f"{qwhere} 缺 question")
            try:
                category = int(qa.get("category"))
            except (TypeError, ValueError):
                raise SchemaError(f"{qwhere} category 不是整数: {qa.get('category')!r}")
            if category not in LOCOMO_CATEGORIES:
                raise SchemaError(f"{qwhere} 未知类别: {category}")
            category_counts[category] += 1
            qa_total += 1

            has_answer = qa.get("answer") not in (None, "")
            has_adv = qa.get("adversarial_answer") not in (None, "")
            if category == 5:
                if not (has_answer or has_adv):
                    anomalies.append(f"{qwhere}: cat5 既无 answer 也无 adversarial_answer")
            elif not has_answer:
                anomalies.append(f"{qwhere}: cat{category} 缺 answer（上游已知标注问题形态）")

            evidence = qa.get("evidence") or []
            if isinstance(evidence, list):
                for ev in evidence:
                    if str(ev) not in dia_ids:
                        anomalies.append(f"{qwhere}: evidence 引用不存在的 dia_id {ev!r}")
            elif evidence:
                anomalies.append(f"{qwhere}: evidence 不是列表")

    return {
        "dataset": "locomo",
        "samples": len(samples),
        "qa_total": qa_total,
        "category_counts": {k: category_counts[k] for k in sorted(category_counts)},
        "anomalies": anomalies,
    }
