"""benchmarks.corrections — 版本化修正清单（兑现 PROTOCOL.md §1 的承诺）。

上游数据的标注问题（LoCoMo 的 evidence 引用缺失、cat5 只有
``adversarial_answer`` 等）由 ``benchmarks.schemas`` 如实上报进
``schema_report.anomalies``，**原始数据一个字节不改**。若评分确实需要修正，
只能走这里，并且受三道硬约束：

1. **必须有版本号**（``manifest_version``）。没有版本号的"修正清单"是一个
   可以随时悄悄变的活文件——公布出去的成绩就永远无法复核。
2. **非空清单必须钉住数据哈希**（``applies_to_sha256``）。否则同一份修正会被
   顺手套到另一份数据上，那不是修正，是拿 A 的修正去改 B 的分数。
   清单为空时不要求钉哈希：空清单改不动任何数字，钉了也没有意义——
   **这道钉的门槛正好落在"能改动数字"的那一刻**。
3. **只允许重述上游标注，不允许改答案正文**。补 evidence 引用、把官方口径的
   拒答题标出来，都属于"上游漏标了，我们补上并留痕"；改 ``answer`` /
   ``question`` 则是造数据。这条在 schema 层拦死，不靠自觉。

**方向上的诚实交代**：``mark_adversarial`` 把一道题从召回分母里拿掉，
只可能抬高我们的数字；``add_evidence`` 则两个方向都可能。正因为前者对我们
单向有利，才更要钉住哈希、写明理由、并强制零修正基线对照——三样都是硬闸门，
不是建议。

还有一条不那么显眼但同样要紧的：**匹配不到目标的修正必须报错**，不许静默
跳过。一条已经失效的修正如果只是默默什么都不做，清单就会慢慢腐烂成一堆
无人知其是否生效的条目，而报告里照样写着"已应用修正清单 vN"。

定位用 ``sample_index`` / ``qa_index``——下标只有在文件逐字节固定时才有意义，
这也正是非空清单必须钉哈希的另一半理由。

敏感性分析（含/不含修正各报一遍）不在本模块，而在 ``benchmarks/run.py`` 的
formal 闸门：带着非空修正跑正式成绩，必须同时给出一次不含修正的基线运行，
否则拒绝启动。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from typing import Any

SCHEMA_VERSION = 1

#: 允许的修正动作。白名单，不是黑名单——新动作必须显式加进来并配上校验。
ALLOWED_OPS = ("add_evidence", "mark_adversarial")

#: 一条修正里允许出现的键。多一个键就拒收：拼错的键名如果被忽略，
#: 修正就会变成"看起来写了、其实没生效"。
_OP_KEYS = {
    "add_evidence": {"op", "sample_index", "qa_index", "dia_ids", "why"},
    "mark_adversarial": {"op", "sample_index", "qa_index", "why"},
}

#: 任何一条修正都不许碰这些字段——碰了就不是修正，是造数据。
FORBIDDEN_FIELDS = ("answer", "question", "adversarial_answer", "text")


class CorrectionError(Exception):
    """修正清单不合法，或应用时对不上目标。一律拒绝，不降级继续。"""


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _require(cond: Any, msg: str) -> None:
    if not cond:
        raise CorrectionError(msg)


def _validate_op(op: Any, where: str) -> dict:
    _require(isinstance(op, dict), f"{where} 不是对象")
    kind = op.get("op")
    _require(
        kind in ALLOWED_OPS,
        f"{where} 的 op 不在白名单里: {kind!r}（允许: {', '.join(ALLOWED_OPS)}）",
    )
    # 顺序要紧：正文字段本身也属于"未知键"，若先报未知键，
    # 「想改答案」这条红线就会被一句笼统的"拼错了"盖掉。先报最具体的那条。
    for field in FORBIDDEN_FIELDS:
        _require(
            field not in op,
            f"{where} 试图改 {field!r}——修正清单只许重述标注，改答案正文是造数据",
        )
    extra = set(op) - _OP_KEYS[kind]
    _require(not extra, f"{where} 出现未知键: {sorted(extra)}——拼错的键会被忽略，故拒收")
    for key in ("sample_index", "qa_index"):
        _require(
            isinstance(op.get(key), int) and op[key] >= 0,
            f"{where} 的 {key} 必须是非负整数",
        )
    _require(
        isinstance(op.get("why"), str) and op["why"].strip(),
        f"{where} 缺 why——没写理由的修正不许进清单",
    )
    if kind == "add_evidence":
        dia_ids = op.get("dia_ids")
        _require(
            isinstance(dia_ids, list) and dia_ids
            and all(isinstance(d, str) and d.strip() for d in dia_ids),
            f"{where} 的 dia_ids 必须是非空字符串列表",
        )
    return op


def load_corrections(path: str, *, dataset: str, data_sha256: str | None = None) -> dict:
    """读并校验一份修正清单。不合法直接抛 CorrectionError。

    ``data_sha256`` 是本次实际要跑的数据文件哈希。清单非空时必须与
    ``applies_to_sha256`` 相等；清单为空时不检查（空清单改不动任何数字）。
    """
    _require(os.path.exists(path), f"修正清单不存在: {path}")
    with open(path, encoding="utf-8") as f:
        try:
            manifest = json.load(f)
        except json.JSONDecodeError as exc:
            raise CorrectionError(f"修正清单不是合法 JSON: {exc}") from exc

    _require(isinstance(manifest, dict), "修正清单必须是对象")
    _require(
        manifest.get("schema_version") == SCHEMA_VERSION,
        f"修正清单 schema_version 必须是 {SCHEMA_VERSION}，"
        f"实际 {manifest.get('schema_version')!r}",
    )
    version = manifest.get("manifest_version")
    _require(
        isinstance(version, str) and version.strip(),
        "修正清单缺 manifest_version——没版本号的清单可以随时悄悄变，成绩无法复核",
    )
    _require(
        manifest.get("dataset") == dataset,
        f"修正清单是给 {manifest.get('dataset')!r} 的，本次跑的是 {dataset!r}",
    )
    corrections = manifest.get("corrections")
    _require(isinstance(corrections, list), "修正清单的 corrections 必须是列表")

    for i, op in enumerate(corrections):
        _validate_op(op, f"corrections[{i}]")

    pinned = str(manifest.get("applies_to_sha256", "")).strip()
    if corrections:
        _require(
            pinned and "PENDING" not in pinned.upper(),
            f"清单 {version} 有 {len(corrections)} 条修正却没钉数据哈希"
            "（applies_to_sha256 缺失或 PENDING）——能改动数字的修正必须钉住它改的是哪份数据",
        )
        if data_sha256:
            _require(
                pinned == data_sha256,
                f"清单 {version} 钉的是 {pinned[:16]}…，本次数据是 {data_sha256[:16]}…"
                "——拿这份修正去改另一份数据的分数，不予放行",
            )

    return {
        "path": os.path.basename(path),
        "manifest_version": version,
        "manifest_sha256": _sha256_file(path),
        "dataset": dataset,
        "applies_to_sha256": pinned or None,
        "count": len(corrections),
        "ops": [c["op"] for c in corrections],
        "corrections": corrections,
    }


def _locate_qa(instances: Any, op: dict, where: str) -> tuple[dict, dict]:
    si, qi = op["sample_index"], op["qa_index"]
    _require(
        isinstance(instances, list) and si < len(instances),
        f"{where} 指向 sample_index={si}，数据里只有 "
        f"{len(instances) if isinstance(instances, list) else 0} 个样本",
    )
    sample = instances[si]
    qa_list = sample.get("qa") if isinstance(sample, dict) else None
    _require(
        isinstance(qa_list, list) and qi < len(qa_list),
        f"{where} 指向 qa_index={qi}，sample[{si}] 只有 "
        f"{len(qa_list) if isinstance(qa_list, list) else 0} 道题",
    )
    return sample, qa_list[qi]


def _sample_dia_ids(sample: dict) -> set[str]:
    found: set[str] = set()
    for key, value in (sample.get("conversation") or {}).items():
        if not key.startswith("session_") or not isinstance(value, list):
            continue
        for turn in value:
            if isinstance(turn, dict) and "dia_id" in turn:
                found.add(str(turn["dia_id"]))
    return found


def apply_corrections(dataset: str, instances: Any, loaded: dict) -> tuple[Any, dict]:
    """在**内存副本**上应用修正，返回（修正后的数据, 应用报告）。

    磁盘上的原始数据一个字节都不动——这是 PROTOCOL.md §1 的底线。
    任何一条修正对不上目标（下标越界、dia_id 在该样本里不存在、类别不符）
    一律抛错：一条静默失效的修正比没有这条修正更坏，它会让报告说谎。
    """
    corrections = loaded.get("corrections") or []
    if not corrections:
        # 空清单是常态而非异常：上游标注暂时不需要修正。如实返回原数据。
        return instances, {"applied": 0, "manifest_version": loaded["manifest_version"],
                           "details": []}
    _require(dataset == "locomo",
             f"目前只实现了 locomo 的修正动作，收到 dataset={dataset!r}")

    corrected = copy.deepcopy(instances)
    details = []
    for i, op in enumerate(corrections):
        where = f"corrections[{i}]"
        sample, qa = _locate_qa(corrected, op, where)
        if op["op"] == "add_evidence":
            known = _sample_dia_ids(sample)
            missing = [d for d in op["dia_ids"] if d not in known]
            _require(
                not missing,
                f"{where} 要补的 dia_id 在 sample[{op['sample_index']}] 里不存在: "
                f"{missing}——补一个不存在的证据只会凭空拉高召回",
            )
            before = [str(e) for e in (qa.get("evidence") or [])]
            added = [d for d in op["dia_ids"] if d not in before]
            _require(
                added,
                f"{where} 要补的 evidence 已经全在题里了——这条修正已失效，"
                "请从清单里删掉（静默跳过会让清单腐烂）",
            )
            qa["evidence"] = before + added
            details.append({"op": "add_evidence", "at": [op["sample_index"], op["qa_index"]],
                            "added": added, "why": op["why"]})
        else:  # mark_adversarial
            _require(
                int(qa.get("category", -1)) == 5,
                f"{where} 要把 sample[{op['sample_index']}].qa[{op['qa_index']}] 标成拒答题，"
                f"但它的 category 是 {qa.get('category')!r}——官方口径只有 cat5 走拒答判定",
            )
            _require(
                not qa.get("_marked_adversarial"),
                f"{where} 重复标注同一道题——这条修正已失效，请从清单里删掉",
            )
            qa["_marked_adversarial"] = True
            details.append({"op": "mark_adversarial",
                            "at": [op["sample_index"], op["qa_index"]], "why": op["why"]})

    return corrected, {"applied": len(details),
                       "manifest_version": loaded["manifest_version"],
                       "details": details}
