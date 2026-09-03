"""
ducky.conflict_resolver — 显式冲突消解器 (v17.0 · 借鉴 Mímir 联邦记忆系统)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
用于检测并消解事实/偏好中的显式矛盾与新旧替换。

借鉴来源: Mímir v9.1 联邦记忆系统 (Sandro 项目)
  - 属性级 Key-Value 覆盖检测（同 category+key 的旧值打 valid_to 失效降权）
  - 显式互斥规则消解（规则集可扩展，代替向量相似度盲覆盖）
  - 与 v12 Chronos 双时间轴协同：软失效降权不删除，历史可溯

包含:
1. 属性级同域新旧覆盖 (Key-Value Override Detection)
2. 反义词与互斥状态碰撞检测 (Antonym & Mutual Exclusion Resolution)
3. 过期降权机制 (valid_to 标记 + 显著性衰减)
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from ducky.utils import DEFAULT_USER_ID, get_facts_conn
from ducky.bank_contract import (
    DEFAULT_BANK_ID,
    ensure_bank_registered,
    ensure_memory_banks_schema,
    legacy_fact_scope_predicate,
    make_scope,
    table_columns,
)

logger = logging.getLogger("aiduMEM.ConflictResolver")

# ── 互斥属性规则集（可扩展，脱敏版本，不含具体业务内容）──────────────────────────
# 格式: (属性类型正则, 旧值模式正则, 新值模式正则)
# 当新文本匹配 new_re 时，自动失效数据库中匹配 old_re 的条目
MUTUAL_EXCLUSION_PATTERNS: list[tuple[str, str, str]] = [
    # 🔴v20.3.2-beta（外审）：**这里原有两条带 `*_placeholder` 的规则。**
    # 开源脱敏时只做了字符串替换，于是 `old_domain_placeholder` 这种正则
    # **永不匹配任何真实文本** —— 4 条规则里 2 条是死代码，而对外宣称吸收了
    # 「显式互斥消解」特性。更难堪的是逃生阀 load_custom_exclusion_patterns()
    # 一直存在、CHANGELOG 也写着「供 api_server 启动时配置」，却**全仓无人调用**
    # （「定义了不接线」同型病）。
    # 现在：占位符规则删除，域名/名称类互斥改由部署方通过下面的注入 API 提供
    # （见 load_custom_exclusion_patterns 的 docstring 与 api_server 启动接线）。
    # 状态开关（双向）
    (r"(开关|状态|status|mode)", r"(开启|启用|open|enable)", r"(关闭|禁用|close|disable)"),
    (r"(开关|状态|status|mode)", r"(关闭|禁用|close|disable)", r"(开启|启用|open|enable)"),
]


def _fact_scope_sql(conn: Any, scope) -> tuple[str, list[Any]]:
    """Build a scope predicate against the columns actually present.

    v20：语义与 :func:`ducky.bank_contract.legacy_fact_scope_predicate`
    完全一致 —— 四列齐全时直接委托它，保证冲突消解与读路径用同一把尺子：

    - 具名域一律严格 ``bank_id + user_id``，没有任何渠道回落；
    - 默认域内，``source``/``agent_id`` 渠道标记只对「尚无主人」的行
      （user_id 为 NULL/空白/占位 default）生效。初版把渠道标记与
      ``user_id`` 平铺成 OR，等于渠道字段可以对**已有主人**的行发起
      失效写（invalidate），是一条跨租户写口子。

    列感知回退只服务于极老的维护/测试库（缺 source/agent_id 甚至
    user_id）；两处调用点都先跑 ``ensure_memory_banks_schema``，生产路径
    永远走契约谓词。没有 bank_id 列的表存不下具名域的行，此时对具名域
    的消解应当一行都命不中（1=0），而不是退回全库。
    """
    # 统一走 bank_contract.table_columns：那里已经把「PRAGMA 失败」与
    # 「表不存在」分开了（后者返回空集不抛异常），手写一遍就会把真故障
    # 静默翻译成「老库没有作用域列」，从而把消解退回全库口径。
    columns = table_columns(conn, "facts")

    if {"user_id", "bank_id", "source", "agent_id"} <= columns:
        sql, params = legacy_fact_scope_predicate(scope)
        return sql.removeprefix(" AND "), list(params)

    channel_terms: list[str] = []
    channel_params: list[Any] = []
    if "source" in columns:
        channel_terms.append("source=?")
        channel_params.append(scope.user_id)
    if "agent_id" in columns:
        channel_terms.append("agent_id=?")
        channel_params.append(scope.user_id)

    if "user_id" not in columns:
        # 无归属列的老库：只能靠渠道字段近似（v19 行为），什么列都没有
        # 时放行 —— 那样的库里根本不存在多租户。
        if channel_terms:
            return "(" + " OR ".join(channel_terms) + ")", channel_params
        return "1=1", []

    if scope.bank_id != DEFAULT_BANK_ID and "bank_id" not in columns:
        return "1=0", []

    clauses: list[str] = []
    params: list[Any] = []
    if "bank_id" in columns:
        if scope.bank_id != DEFAULT_BANK_ID:
            return "bank_id=? AND user_id=?", [scope.bank_id, scope.user_id]
        clauses.append("bank_id=?")
        params.append(scope.bank_id)

    unclaimed = "(user_id IS NULL OR TRIM(user_id)='' OR user_id=?)"
    if channel_terms:
        clauses.append(
            f"(user_id=? OR ({unclaimed} AND "
            f"({' OR '.join(channel_terms)})))"
        )
        params += [scope.user_id, DEFAULT_USER_ID] + channel_params
    else:
        # 没有渠道列就没有回落证据：只认正规归属。
        clauses.append("user_id=?")
        params.append(scope.user_id)
    return " AND ".join(clauses), params


def load_custom_exclusion_patterns(patterns: list[tuple[str, str, str]]) -> None:
    """
    运行时注入自定义互斥规则（替换占位符或追加规则）。
    在 api_server 启动时调用，注入具体业务的域名/名称变动规则。

    示例:
        load_custom_exclusion_patterns([
            (r"(域名|url)", r"old\\.example\\.com", r"new\\.example\\.com"),
        ])
    """
    global MUTUAL_EXCLUSION_PATTERNS
    # 追加运行时规则（不覆盖基础规则）
    MUTUAL_EXCLUSION_PATTERNS = [
        p for p in MUTUAL_EXCLUSION_PATTERNS
        if "placeholder" not in p[1]  # 移除未初始化的占位符
    ] + patterns
    logger.info("🐙 [ConflictResolver] 已加载 %d 条自定义互斥规则", len(patterns))


def load_custom_exclusion_patterns_from_file(path: str | None = None) -> int:
    """v20.3.2 正式版（E1）：把「定义了不接线」的逃生阀真正接到部署面。

    读 `DATA_DIR/conflict_rules.json`，格式二选一：
        {"mutual_exclusion": [{"attr": "(域名|url)", "old": "old\\.example", "new": "new\\.example"}, ...]}
        [["(域名|url)", "old\\.example", "new\\.example"], ...]
    文件不存在 → 0（正常，多数部署没有自定义规则）；JSON 坏 → WARNING 且不装；
    单条正则坏 → WARNING 跳过该条，其余照装。返回实际装入条数。
    """
    import json
    import os
    if path is None:
        from ducky.utils import DATA_DIR
        path = os.path.join(DATA_DIR, "conflict_rules.json")
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:
        logger.warning("🐙 [ConflictResolver] conflict_rules.json 不可读，未装载自定义规则：%s", exc)
        return 0
    items = raw.get("mutual_exclusion", []) if isinstance(raw, dict) else raw
    good: list[tuple[str, str, str]] = []
    for i, item in enumerate(items or []):
        try:
            if isinstance(item, dict):
                triple = (str(item["attr"]), str(item["old"]), str(item["new"]))
            else:
                triple = (str(item[0]), str(item[1]), str(item[2]))
            for rx in triple:
                re.compile(rx)
            good.append(triple)
        except Exception as exc:
            logger.warning("🐙 [ConflictResolver] conflict_rules.json 第 %d 条无效，跳过：%s", i, exc)
    if good:
        load_custom_exclusion_patterns(good)
    return len(good)


# ── 纠正语检测（v20.2.4 · WP-B）──
#
# 观察：用户明说「你记错了 / 不对，是…」是信噪比最高的一类信号。
#
# 🔴 **红线（不许删这段注释）**：**纠正语绝不允许单独触发替换或删除。**
# 正则必然误报（「这个方案不对称」「我没错过」），而记忆删除不可逆——
# 用户随口一句就丢记忆，是本系统最不能犯的错。本函数是**纯谓词**：
# 它只回答「这句话像不像纠正」，不碰库、不改判决、不产生副作用。
#
# 收窄手段是**位置约束**：纠正语是话头，必须出现在句首或标点之后。
# 「这个方案不对称」里的「不对」前面是「案」——不匹配。宁可漏报不可误报。
_CORRECTION_RE = re.compile(
    r"(?:^|[，,。.；;：:！!？?\s])"           # 话头位：句首或标点后
    r"(?:"
    r"不对(?![称等劲口路头号板])"              # 排除「不对称/不对等/不对劲」等构词
    r"|不是这样|不是的"
    r"|(?:你|我)(?:记|说|理解)错了?"
    r"|搞错了?|弄错了?|说错了?|记错了?"
    r"|更正一下|纠正一下|更正[:：]|纠正[:：]"
    r"|(?:我)?改主意了?"
    r"|actually[,，]"                          # 必须带逗号：排除「actually working」
    r"|correction[:：]"
    r"|I\s+was\s+wrong"
    r"|that'?s\s+(?:not\s+right|wrong|incorrect)"
    r"|no[,，]\s*(?:it'?s|it\s+is|the|that)"
    r")",
    re.IGNORECASE,
)


def is_correction(text: str) -> bool:
    """这句话是否带用户明示的纠正措辞。**纯谓词，零副作用。**

    调用方必须自己承担判决责任 —— 见上方红线：本函数为真**不构成**
    删除或替换任何记忆的理由。
    """
    if not text:
        return False
    return bool(_CORRECTION_RE.search(str(text)))


def resolve_fact_conflict(
    category: str,
    fact_key: str,
    new_value: str,
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> dict[str, Any]:
    """
    当写入/更新某个 (category, fact_key) 时：
    1. 检查是否存在旧的同 category & fact_key 但 value 不同的有效记录；
    2. 若存在，将旧记录的 valid_to 设为当前时间（软失效降权而非物理删除）；
    3. 同时写入 fact_events 变更账本（可溯源）；
    4. 返回消解结果。
    """
    scope = make_scope(user_id, bank_id)
    conn = get_facts_conn()
    now_str = datetime.now(timezone.utc).isoformat()
    invalidated_count = 0
    invalidated_ids: list[int] = []
    try:
        # Ensure old facts/event tables have canonical scope columns before
        # any read or write.  This is additive and safe to repeat per call.
        ensure_memory_banks_schema(conn)
        ensure_bank_registered(scope, conn)
        cursor = conn.cursor()
        scope_sql, scope_params = _fact_scope_sql(conn, scope)
        # 仅查找有效状态下 key 相同但 value 不同的记录（利用索引 idx_facts_unique）
        rows = cursor.execute(
            "SELECT id, fact_value FROM facts "
            "WHERE category = ? AND fact_key = ? "
            "AND (valid_to IS NULL OR valid_to > ?) AND " + scope_sql,
            [category, fact_key, now_str] + scope_params,
        ).fetchall()

        for fid, old_val in rows:
            if old_val != new_value:
                cursor.execute(
                    "UPDATE facts SET valid_to = ?, updated_at = ? WHERE id = ? AND "
                    + scope_sql,
                    [now_str, now_str, fid] + scope_params,
                )
                invalidated_count += 1
                invalidated_ids.append(fid)
                logger.info(
                    "🐙 [ConflictResolver] 属性级覆盖: key='%s' old='%s' -> new='%s' (id=%d 已失效)",
                    fact_key, old_val[:80], new_value[:80], fid,
                )

        # 写入变更账本（如果有消解动作）
        if invalidated_ids:
            _append_conflict_event(
                cursor,
                category,
                fact_key,
                new_value,
                invalidated_ids,
                now_str,
                user_id=scope.user_id,
                bank_id=scope.bank_id,
            )

        conn.commit()
    except Exception as e:
        logger.error("🐙 [ConflictResolver] resolve_fact_conflict 失败: %s", e)
    finally:
        conn.close()

    return {
        "invalidated": invalidated_count,
        "category": category,
        "fact_key": fact_key,
        "user_id": scope.user_id,
        "bank_id": scope.bank_id,
    }


def _attr_matches(new_text: str, fact_key: str, attr_re: str) -> bool:
    """新文本是否**确实在说这条事实的那个属性**（v20.2.4 · 外审 F-13 第二层）。

    第一层（attr_re 在新文本里）只能证明「这句话在谈某个开关」，证明不了
    「在谈哪一个」。所以再要一级显式判据：

      ① fact_key 整体出现在新文本里 —— 最强证据；
      ② fact_key 剥掉通用状态词后的**具名部分**出现在新文本里
         （「邮箱开关」剥掉「开关」剩「邮箱」）。

    两级都不满足就**不消解**。记忆失效不可逆，宁漏一条不错杀一条 ——
    这条取舍写在这里，别为了「多消解一点」把它放宽。
    """
    key = (fact_key or "").strip()
    if not key:
        return False
    if key in new_text:
        return True
    specific = re.sub(attr_re, "", key, flags=re.IGNORECASE).strip()
    return bool(specific) and specific in new_text


def scan_and_resolve_text_conflicts(
    new_text: str,
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> list[dict[str, Any]]:
    """
    针对输入的文本内容，检测是否触及显式互斥规则。
    若新文本匹配到新规则模式，则扫描 facts DB 中匹配旧规则的条目，
    将其标记 valid_to = NOW()（软失效，不删除）。

    性能优化: 先在内存中做规则匹配，只在命中时才查数据库。
    """
    # v20.2.4（WP-B）：纠正语**只登记，不判决**。
    #
    # 取舍说明：本函数原有一条「无规则命中就不碰 DB」的快速返回路径。为记一笔
    # 账而在这里开库，会把零开销的快速路径变成每次写入都打库 —— 用性能倒退换
    # 一条统计，不划算。所以登记分两处落：
    #   · 无消解时 → 只落日志（正则是微秒级，不碰 DB）；
    #   · 有消解时 → 附在**已经要写的**那条账本行上（零新连接、零新写入、零表变更）。
    #
    # 边界：属性级入口 resolve_fact_conflict() 不接此信号 —— 它只拿到
    # (category, fact_key, value)，原文不在它手上，纠正措辞无从谈起。
    signaled = is_correction(new_text)

    # 先判断文本中是否有任何规则被命中（避免无效 DB 查询）
    # v20.2.4（外审 F-13）：**attr_re 此前从未参与判定**。规则是三段
    # (属性, 旧值, 新值)，而这里只看 new_re、下面只用 old_re —— 属性正则解包出来
    # 就被丢掉了。后果实测：提交「请关闭通知（与邮箱和灯光都无关）」，同域两条
    # 互不相关的「已开启」事实**同时被失效**。通用状态词成了域内广谱杀虫剂。
    #
    # 第一层收窄：属性词也必须出现在新文本里。上面那句话没有「开关/状态」字样，
    # 到这里就被拦住了。
    triggered_patterns = [
        (attr_re, old_re, new_re)
        for attr_re, old_re, new_re in MUTUAL_EXCLUSION_PATTERNS
        if re.search(new_re, new_text, re.IGNORECASE)
        and re.search(attr_re, new_text, re.IGNORECASE)
    ]
    if not triggered_patterns:
        if signaled:
            # 这正是「信号被浪费」的那个场景：用户明说记错了，而内容层没有
            # 任何互斥规则接得住。**如实留痕，但一个字都不改** —— 见 is_correction
            # 上方红线：正则必然误报，而记忆删除不可逆。
            logger.info(
                "🐙 [ConflictResolver] 检测到用户纠正措辞，内容层无规则命中；"
                "仅登记不消解: %.60s", new_text,
            )
        return []  # 快速返回，不查 DB

    scope = make_scope(user_id, bank_id)
    resolved_actions: list[dict[str, Any]] = []
    now_str = datetime.now(timezone.utc).isoformat()
    conn = get_facts_conn()
    try:
        ensure_memory_banks_schema(conn)
        ensure_bank_registered(scope, conn)
        cursor = conn.cursor()
        scope_sql, scope_params = _fact_scope_sql(conn, scope)
        # 仅对命中的规则做针对性查询
        for attr_re, old_re, new_re in triggered_patterns:
            rows = cursor.execute(
                "SELECT id, fact_key, fact_value FROM facts "
                "WHERE (valid_to IS NULL OR valid_to > ?) AND "
                + scope_sql
                + " AND (fact_value REGEXP ? OR fact_key REGEXP ?)",
                [now_str] + scope_params + [old_re, old_re],
            ).fetchall()
            # SQLite 不原生支持 REGEXP，回退到 Python 过滤
            if not rows:
                rows = cursor.execute(
                    "SELECT id, fact_key, fact_value FROM facts "
                    "WHERE (valid_to IS NULL OR valid_to > ?) AND " + scope_sql,
                    [now_str] + scope_params,
                ).fetchall()
                rows = [
                    r for r in rows
                    if re.search(old_re, str(r[2]), re.IGNORECASE)
                    or re.search(old_re, str(r[1]), re.IGNORECASE)
                ]

            for fid, fkey, fval in rows:
                if not _attr_matches(new_text, fkey, attr_re):
                    logger.debug(
                        "🐙 [ConflictResolver] 属性对不上，跳过消解: key='%s'", fkey,
                    )
                    continue
                cursor.execute(
                    "UPDATE facts SET valid_to = ?, updated_at = ? WHERE id = ? AND "
                    + scope_sql,
                    [now_str, now_str, fid] + scope_params,
                )
                resolved_actions.append({
                    "fact_id": fid,
                    "fact_key": fkey,
                    "old_value": fval,
                    "user_id": scope.user_id,
                    "bank_id": scope.bank_id,
                    "reason": f"规则触发: {old_re} -> {new_re}",
                })
                logger.info(
                    "🐙 [ConflictResolver] 规则消解: id=%d key='%s' 旧值='%.60s' 因新规则 '%s' 失效",
                    fid, fkey, fval, new_re,
                )

        if resolved_actions:
            _append_conflict_event(
                cursor, "scan_resolve", new_text[:100],
                f"{len(resolved_actions)} facts invalidated"
                + (" · user_signaled_correction" if signaled else ""),
                [a["fact_id"] for a in resolved_actions], now_str,
                user_id=scope.user_id,
                bank_id=scope.bank_id,
            )
        conn.commit()
    except Exception as e:
        logger.error("🐙 [ConflictResolver] scan_and_resolve_text_conflicts 失败: %s", e)
    finally:
        conn.close()

    return resolved_actions


def _append_conflict_event(
    cursor: Any,
    category: str,
    fact_key: str,
    new_value: str,
    invalidated_ids: list[int],
    now_str: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> None:
    """
    向 fact_events 变更账本追加一条冲突消解记录（借鉴 Mímir 事件账本设计）。
    若 fact_events 表不存在则静默跳过（兼容旧 schema）。
    """
    try:
        cursor.execute(
            """
            INSERT OR IGNORE INTO fact_events
                (event_type, category, fact_key, new_value, affected_ids, created_at, user_id, bank_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "conflict.resolved",
                category,
                fact_key,
                new_value[:200],
                str(invalidated_ids),
                now_str,
                user_id,
                bank_id,
            ),
        )
    except Exception:
        pass  # fact_events 表不存在时静默跳过，向前兼容
