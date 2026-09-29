"""ducky.salience.conflict — 同作用域同泳道的矛盾检测（f0.3 重写）

f0.3 之前的检测器是**子串级**反义词两两比对，而它的输出直接驱动淘汰名单：

  · 单字反义对（开/关、对/错、真/假、新/旧、快/慢、大/小、多/少）几乎在任意
    两句话里都能各命中一半；
  · 「不是」本身含「是」、「不要」本身含「要」—— 两条都写着「不要……」的记忆
    被判成「要 ↔ 不要」矛盾；
  · 从不看两条记忆说的是不是同一件事；
  · 每命中一对，两条记忆的显著性各 ×0.5，一条记忆卷进 k 对就是 ×0.5^k。

生产实测：约 2,600 条记忆，每轮 88,424 对「矛盾」，被腰斩的显著性再经
decay_all 流进淘汰候选。

现在一对记忆要同时过三道闸才算矛盾：

  判据的比较单位是**子句**（按标点切开），下面三道闸都在同一对子句上判。

  1. **极性真的相反**（两种证据，都要求子句内排他）：
     · 否定式：一条肯定地说了 P，另一条说「不P / 没P / 未P」—— P 取否定词后
       紧跟的 1~2 个字（跳过「太 / 再 / 很 / 怎么」这类程度副词）。于是「不是 /
       不要 / 没有 / 不能」按否定式处理，**绝不再**被当成「是 / 要 / 有 / 能」；
       「不过 / 不断 / 不如 / 未来……」这类不表否定的词不算否定词。
     · 多字反义词对（成功/失败、启用/禁用、允许/禁止、记得/忘记）；单字对全部删除。
     排他：肯定的一方自己不能也否定 P，否定的一方自己不能也肯定 P
     （「不喜欢咖啡但喜欢茶」对「不喜欢咖啡」不是矛盾）。
  2. **说的是同一件事**，三条都要满足：
     · 去掉否定词（反义词对另去掉那两个词）之后，两个子句的词元（中文字二元组、
       ASCII 整词）Jaccard ≥ _SAME_TOPIC_MIN_JACCARD；
     · **主语相同**：翻转点（P / 反义词）之前的文字至少共享一个非纯数字词元，
       或两边都没有前文。「大叔喜欢喝咖啡」对「猴哥不喜欢喝咖啡」Jaccard 恰好
       0.5，只靠 Jaccard 会判成矛盾 —— 可那是两个人；
     · **宾语相同**：翻转点之后的文字同理。「杭州生产机不喜欢加班」对「杭州生产机
       喜欢大屏幕」主语、谓词都一样，说的却是两件事。
  3. **同作用域同泳道**（v20 P0-2 起的老规矩，不变）。

处置档位 AIDUMEI_CONFLICT_PENALTY_MODE（环境变量优先，其次 .env）：
  off   不扫描；
  warn  （默认）扫描并记摘要，**不动显著性**；
  apply 每条卷入矛盾的记忆**本轮最多 ×0.5 一次**，不再按对数叠乘。
两两比对按 (作用域, 泳道) 组设上限 AIDUMEI_CONFLICT_MAX_PAIRS_PER_LANE（默认
200000），超限截断并如实报出 truncated —— 少查了要说出来，不能冒充查全了。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from collections import Counter
from typing import NamedTuple, Optional

from ducky.bank_contract import is_legacy_schema_error
from ducky.env_config import int_env
from ducky.utils import DEFAULT_USER_ID, env_or_env_file, get_salience_conn

logger = logging.getLogger("aiduMEM.salience")


def _canon_uid(uid: str) -> str:
    """把改名后的默认身份与字面量 'default' 折叠为同一组。

    存量 salience 行回填的是字面量 'default'，而新写入盖的是
    AIDUMEM_DEFAULT_USER_ID（部署方可能改名成 alice）。不折叠的话，
    同一个真实域的新旧记忆会被拆成两组，老记忆的矛盾从此漏检
    （与 reflect._identity_ids 的 v19.4.2 教训同源，只放宽分组不改数据）。
    """
    return "default" if uid == DEFAULT_USER_ID else uid


# ── 判据常量 ─────────────────────────────────────────────────────────────
# 只留多字反义词对。单字对（开/关、对/错、真/假、新/旧、快/慢、大/小、多/少）
# 与「要/不要、是/不是、有/没有、能/不能」一并删除：后四组由下面的否定式判据
# 接管，并且接管得更对 ——「不要熬夜」里的「要」不再被当成肯定形态。
_ANTONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("启用", "禁用"), ("允许", "禁止"), ("记得", "忘记"), ("成功", "失败"),
)
_ANTONYM_TERMS = frozenset(t for pair in _ANTONYM_PAIRS for t in pair)

# 否定词（单字）。「没有」= 没 + 有：否定的对象就是「有……」。
_NEGATORS = frozenset("不没未")
# 以否定字开头、但本身不表否定的词：遇到它们不算否定词。
_NON_NEGATING = frozenset({
    "不过", "不断", "不仅", "不但", "不管", "不论", "不然", "不如", "不久",
    "不禁", "不妨", "不免", "不止", "未来",
})
# 否定词与被否定的谓词之间常插的程度副词（「不太喜欢」「不再用」）。
_DEGREE_ADVERBS = ("怎么", "太", "再", "很")
_PHRASE_LEN = 2                   # 被否定的短语 P 取否定词后的 1~2 个字
_SAME_TOPIC_MIN_JACCARD = 0.5     # 「说的是同一件事」的字二元组 Jaccard 下限
_NORMALIZE_RE = re.compile(r"[^0-9a-z\u3400-\u9fff]+")   # 只留数字/小写字母/CJK

_CONFLICT_PENALTY = 0.5  # apply 档：卷入矛盾的记忆本轮显著性 ×0.5（每条最多一次）

# 环境变量名必须是字面量（ducky.env_registry 按 AST 字符串常量登记）。
_PENALTY_MODE_ENV = "AIDUMEI_CONFLICT_PENALTY_MODE"
_MAX_PAIRS_ENV = "AIDUMEI_CONFLICT_MAX_PAIRS_PER_LANE"
PENALTY_MODES = ("off", "warn", "apply")
DEFAULT_PENALTY_MODE = "warn"
DEFAULT_MAX_PAIRS_PER_LANE = 200_000
_LOG_EXAMPLES = 5                 # 日志只举这么多个例子，其余只进计数


def conflict_penalty_mode_status() -> dict:
    """处置档位的生效值 + 原始值 + 配置错误（非法值回落 warn 并告警）。"""
    raw = env_or_env_file(_PENALTY_MODE_ENV, "")
    value = raw.strip().lower()
    if not value:
        return {"mode": DEFAULT_PENALTY_MODE, "raw": None, "error": None}
    if value in PENALTY_MODES:
        return {"mode": value, "raw": raw, "error": None}
    error = (f"{_PENALTY_MODE_ENV}={raw!r} 不是 off|warn|apply 之一，"
             f"已按默认 {DEFAULT_PENALTY_MODE} 处理（只记摘要，不改显著性）")
    logger.warning("⚠️ %s", error)
    return {"mode": DEFAULT_PENALTY_MODE, "raw": raw, "error": error}


def conflict_penalty_mode() -> str:
    return conflict_penalty_mode_status()["mode"]


def max_pairs_per_lane() -> int:
    """每个 (作用域, 泳道) 组的两两比对上限；非法值回退默认并出声（env_config 纪律）。"""
    return int_env(_MAX_PAIRS_ENV, DEFAULT_MAX_PAIRS_PER_LANE, minimum=1)


# ── 单条记忆的特征（按子句） ─────────────────────────────────────────────
# 判据的比较单位是**子句**（规范化时按标点/空白切开的片段），不是整条记忆：
# 多子句记忆里「其他子句」的共同文字会把整条的 Jaccard 抬高 ——「小猴要先在本地
# 测试，猴哥喜欢大屏幕」对「aiduMEI要先在本地测试，猴哥不喜欢加班」整条 Jaccard
# 过 0.5，可「喜欢大屏幕」和「不喜欢加班」根本不是一件事。
class _Clause(NamedTuple):
    neg: dict          # 被否定的短语 P -> 原文里的否定形态（如 "喜欢" -> "不喜欢"）
    pos: frozenset     # 非否定位置起始的 1~2 字片段（P 的肯定出现）
    terms: frozenset   # 非否定位置出现的反义词
    plain: str         # 去掉否定词后的子句
    grams: frozenset   # plain 的字二元组


class _Features(NamedTuple):
    clauses: tuple     # 各子句的 _Clause
    neg: frozenset     # 以下三项是各子句的并集，只用于廉价预筛
    pos: frozenset
    terms: frozenset


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "").lower()
    return _NORMALIZE_RE.sub(" ", folded).strip()


def _is_negator(seg: str, i: int) -> bool:
    return seg[i] in _NEGATORS and seg[i:i + 2] not in _NON_NEGATING


def _skip_adverb(seg: str, j: int) -> int:
    for adverb in _DEGREE_ADVERBS:
        if seg.startswith(adverb, j):
            return j + len(adverb)
    return j


_ASCII_RUN_RE = re.compile(r"[0-9a-z]+")


def _char_grams(text: str) -> frozenset:
    """相似度用的词元：中文按字二元组（单字片段取单字），ASCII 连续串整体算一个词。

    ASCII 不拆成字母二元组：「consolidator」拆开是 11 个二元组，两条只共享这一个
    英文词的记忆 Jaccard 就能冲过 0.5 —— 相似度被一个长英文名字霸占了。
    """
    out: set[str] = set()
    for seg in text.split():
        out.update(_ASCII_RUN_RE.findall(seg))
        for run in _ASCII_RUN_RE.split(seg):
            if len(run) == 1:
                out.add(run)
            elif run:
                out.update(run[k:k + 2] for k in range(len(run) - 1))
    return frozenset(out)


def _jaccard(a: frozenset, b: frozenset) -> float:
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _split_at(plain: str, token: str) -> tuple[str, str]:
    """(token 第一次出现之前, 之后)：主语大多在前面，宾语/补语在后面。"""
    k = plain.find(token)
    if k < 0:
        return plain, ""
    return plain[:k], plain[k + len(token):]


def _overlaps(a: str, b: str) -> bool:
    """共享至少一个非纯数字词元；两边都是空的也算一致。

    纯数字词元（日期、版本号）不算：两条都以「2026-09-…」开头不说明在说同一件事。
    """
    ga = {g for g in _char_grams(a) if not g.isdigit()}
    gb = {g for g in _char_grams(b) if not g.isdigit()}
    if not ga and not gb:
        return True
    return bool(ga & gb)


def _same_subject_and_object(x_plain: str, x_token: str, y_plain: str, y_token: str) -> bool:
    """翻转点之前（主语）与之后（宾语/补语）两边都要对得上。

    「杭州生产机不喜欢加班」对「杭州生产机喜欢大屏幕」：主语相同、谓词相同，
    子句 Jaccard 0.545 —— 但一个说加班、一个说大屏幕，不是一件事。
    """
    x_pre, x_post = _split_at(x_plain, x_token)
    y_pre, y_post = _split_at(y_plain, y_token)
    return _overlaps(x_pre, y_pre) and _overlaps(x_post, y_post)


def _clause(seg: str) -> _Clause:
    """一个子句（规范化后按标点/空白切出的连续片段）的极性特征。"""
    markers: set[int] = set()
    negated: set[int] = set()
    neg: dict = {}
    for i in range(len(seg)):
        if not _is_negator(seg, i):
            continue
        markers.add(i)
        j = _skip_adverb(seg, i + 1)
        phrase = seg[j:j + _PHRASE_LEN]
        if phrase and phrase[0] not in _NEGATORS:
            neg.setdefault(phrase, seg[i:j + len(phrase)])
            negated.add(j)
    blocked = markers | negated
    pos: set = set()
    for i in range(len(seg)):
        if i not in blocked:
            pos.add(seg[i])
            pos.add(seg[i:i + _PHRASE_LEN])
    terms: set = set()
    for term in _ANTONYM_TERMS:
        start = seg.find(term)
        while start != -1:
            if start not in blocked:
                terms.add(term)
                break
            start = seg.find(term, start + 1)
    plain = "".join(ch for k, ch in enumerate(seg) if k not in markers)
    return _Clause(neg, frozenset(pos), frozenset(terms), plain, _char_grams(plain))


def _features(text: str) -> _Features:
    clauses = tuple(_clause(seg) for seg in _normalize(text).split())
    return _Features(
        clauses,
        frozenset(k for c in clauses for k in c.neg),
        frozenset(g for c in clauses for g in c.pos),
        frozenset(t for c in clauses for t in c.terms),
    )


# ── 一对记忆的判决 ───────────────────────────────────────────────────────
def _negation_hit(cn: _Clause, cp: _Clause) -> Optional[tuple[str, str]]:
    """cn 否定了 P、cp 肯定了 P，且两个子句各自不自相矛盾 → (P, 否定形态)。"""
    for phrase in sorted(cn.neg.keys() & cp.pos):
        if phrase not in cn.pos and phrase not in cp.neg:
            return phrase, cn.neg[phrase]
    return None


def _clause_verdict(ca: _Clause, cb: _Clause) -> Optional[dict]:
    """两个子句是否矛盾：极性相反 + 子句级 Jaccard + 翻转点前后（主语/宾语）一致。"""
    for x, y in ((ca, cb), (cb, ca)):
        hit = _negation_hit(x, y)
        if hit is None:
            continue
        sim = _jaccard(x.grams, y.grams)
        if sim >= _SAME_TOPIC_MIN_JACCARD and _same_subject_and_object(
                x.plain, hit[0], y.plain, hit[0]):
            return {"kind": "negation", "word_pair": f"{hit[0]}↔{hit[1]}",
                    "similarity": round(sim, 3)}
    for pos_t, neg_t in _ANTONYM_PAIRS:
        for x, y in ((ca, cb), (cb, ca)):
            if not (pos_t in x.terms and neg_t not in x.terms
                    and neg_t in y.terms and pos_t not in y.terms):
                continue
            sim = _jaccard(_char_grams(x.plain.replace(pos_t, " ")),
                           _char_grams(y.plain.replace(neg_t, " ")))
            if sim >= _SAME_TOPIC_MIN_JACCARD and _same_subject_and_object(
                    x.plain, pos_t, y.plain, neg_t):
                return {"kind": "antonym", "word_pair": f"{pos_t}↔{neg_t}",
                        "similarity": round(sim, 3)}
    return None


def _judge(fa: _Features, fb: _Features) -> Optional[dict]:
    # 廉价预筛（绝大多数记忆对在这里就出局）：整条记忆层面连候选都没有就不进子句循环
    if not ((fa.neg & fb.pos) or (fb.neg & fa.pos) or (fa.terms and fb.terms)):
        return None
    for ca in fa.clauses:
        for cb in fb.clauses:
            verdict = _clause_verdict(ca, cb)
            if verdict is not None:
                return verdict
    return None


def judge_pair(text_a: str, text_b: str) -> Optional[dict]:
    """两段正文是否构成矛盾：是则返回 {kind, word_pair, similarity}，否则 None。

    与扫描用同一套判据（纯函数，不碰库），给测试与人工复核用。
    """
    return _judge(_features(text_a), _features(text_b))


# ── 扫描 ─────────────────────────────────────────────────────────────────
def _load_rows() -> list[tuple]:
    """(memory_id, lane, content, user_id, bank_id)。

    v20 P0-2：v19 只按 lane 分组，甲库一句「要」会跟乙库一句「不要」配对，
    然后 resolve_conflict_salience 把**两库**的显著性都腰斩——跨库写污染。
    现在配对永远不跨作用域；旧库缺作用域列时退回 v19 查询
    （全库本就是单一 default 域，行为不变）。
    """
    conn = get_salience_conn()
    try:
        try:
            return conn.execute(
                "SELECT memory_id, lane, content_preview, user_id, bank_id "
                "FROM salience WHERE content_preview != ''"
            ).fetchall()
        except sqlite3.Error as exc:
            # 这个降级出口把每一行的作用域**改写**成 ("default","default")。
            # 老库缺作用域列时它是对的（全库本就是单一 default 域）；但原来用
            # except Exception 去接，任何一次查询故障都会让具名域的行被贴上
            # default 标签，于是甲库的「要」重新能跟乙库的「不要」配对，
            # resolve_conflict_salience 再把两库的显著性一起腰斩 —— 正是这段
            # 注释声称已经堵掉的那条跨库写污染。先验明病因。
            if not is_legacy_schema_error(exc):
                raise
            logger.warning("salience 表无作用域列，冲突检测退回 v19 全库口径：%s", exc)
            return [
                (mid, lane, content, "default", "default")
                for mid, lane, content in conn.execute(
                    "SELECT memory_id, lane, content_preview FROM salience WHERE content_preview != ''"
                ).fetchall()
            ]
    finally:
        conn.close()


def _scan_group(key: tuple, items: list, cap: int, stats: dict, out: list) -> None:
    n = len(items)
    total = n * (n - 1) // 2
    stats["pairs_total"] += total
    if n < 2:
        return
    feats = [_features(content) for _mid, content in items]
    budget = min(total, cap)
    compared = 0
    uid, bid, lane = key
    for i in range(n - 1):
        if compared >= budget:
            break
        for j in range(i + 1, n):
            if compared >= budget:
                break
            compared += 1
            verdict = _judge(feats[i], feats[j])
            if verdict is None:
                continue
            (mid_a, ca), (mid_b, cb) = items[i], items[j]
            out.append({
                "lane": lane, "user_id": uid, "bank_id": bid,
                "memory_a": mid_a, "memory_b": mid_b,
                **verdict,
                "preview_a": ca[:60], "preview_b": cb[:60],
            })
    stats["pairs_compared"] += compared
    if compared < total:
        stats["truncated"] = True
        stats["truncated_groups"] += 1


def scan_conflicts(max_pairs: Optional[int] = None) -> dict:
    """扫描同 (user, bank, lane) 组内的矛盾，连同比对规模一起返回。

    返回：conflicts（与 detect_conflicts 同形）、pairs_found、pairs_compared、
    pairs_total、truncated、truncated_groups、groups、memories_scanned、
    max_pairs_per_lane。组内按 memory_id 排序，比对顺序确定、可复现。
    """
    cap = max_pairs if max_pairs is not None else max_pairs_per_lane()
    rows = _load_rows()
    stats = {"memories_scanned": len(rows), "groups": 0, "pairs_total": 0,
             "pairs_compared": 0, "truncated": False, "truncated_groups": 0,
             "max_pairs_per_lane": cap}
    conflicts: list[dict] = []
    # 按 (作用域, lane) 分组——配对绝不跨库
    groups: dict[tuple, list[tuple]] = {}
    for mid, lane, content, uid, bid in rows:
        groups.setdefault((_canon_uid(uid), bid, lane), []).append((mid, content or ""))
    stats["groups"] = len(groups)
    for key in sorted(groups, key=lambda k: tuple(str(x) for x in k)):
        items = sorted(groups[key], key=lambda it: str(it[0]))
        _scan_group(key, items, cap, stats, conflicts)
    return {**stats, "conflicts": conflicts, "pairs_found": len(conflicts)}


def detect_conflicts() -> list[dict]:
    """扫描同 (user, bank, lane) 内的矛盾，返回冲突列表（判据见模块 docstring）。"""
    return scan_conflicts()["conflicts"]


def _log_conflict_summary(conflicts: list[dict], mode: str) -> None:
    kinds = Counter(c.get("kind", "?") for c in conflicts)
    tail = ("；apply 档：每条卷入的记忆本轮显著性最多 ×0.5 一次" if mode == "apply"
            else "；未改动显著性")
    logger.warning("⚔️ 矛盾检测 [%s]：%d 对（否定式 %d / 反义词 %d）%s",
                   mode, len(conflicts), kinds.get("negation", 0),
                   kinds.get("antonym", 0), tail)
    for c in conflicts[:_LOG_EXAMPLES]:
        logger.info("   例 %s | lane=%s | scope=%s/%s | sim=%s | %s ↔ %s",
                    c.get("word_pair"), c.get("lane"),
                    c.get("user_id", "default"), c.get("bank_id", "default"),
                    c.get("similarity"), str(c.get("memory_a"))[:12],
                    str(c.get("memory_b"))[:12])
    if len(conflicts) > _LOG_EXAMPLES:
        logger.info("   …… 其余 %d 对只计数不逐条列出", len(conflicts) - _LOG_EXAMPLES)


def resolve_conflict_salience(conflicts: list[dict], mode: Optional[str] = None) -> int:
    """按处置档位处理检测结果，返回本轮被降显著性的**记忆条数**（去重）。

    mode 不传 = 读 AIDUMEI_CONFLICT_PENALTY_MODE（默认 warn）。off / warn 只记摘要、
    一行都不改；apply 对卷入矛盾的每条记忆本轮 ×0.5 **一次** —— 旧实现按「对」
    叠乘，一条记忆卷进 k 对就被乘 0.5^k。
    """
    effective = conflict_penalty_mode() if mode is None else mode
    if effective not in PENALTY_MODES:
        raise ValueError(f"unknown conflict penalty mode: {effective!r}")
    if not conflicts:
        return 0
    _log_conflict_summary(conflicts, effective)
    if effective != "apply":
        return 0
    ids = sorted({str(mid) for c in conflicts for mid in (c["memory_a"], c["memory_b"])})
    conn = get_salience_conn()
    try:
        conn.executemany(
            "UPDATE salience SET salience = salience * ? WHERE memory_id = ?",
            [(_CONFLICT_PENALTY, mid) for mid in ids],
        )
        conn.commit()
    finally:
        conn.close()
    return len(ids)
