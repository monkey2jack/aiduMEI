"""
ducky.dossier — 记忆档案导出 (v21 preview · EchoMind 融改 F3)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
把一个 (user_id, bank_id) 域的记忆状态渲染成一份人能看懂的 Markdown 档案：
健康总览 / 用户画像 / AI 自己学到的（未经验证）/ 立场观点 / 场景聚类 /
技能结晶 / 检索进化。

设计纪律：
  - **数据提取与纯渲染分离**——render_markdown(data) 零数据库依赖，可独立
    测试（借鉴 EchoMind markdown_renderer 的组织方式，数据源全部本仓自有）。
  - 全部按 (user_id, bank_id) 域收窄；scope 缺失的表（opinions 仅 owner 轴、
    skill_crystals 全局表）在对应章节如实标注口径，不装成域内数据。
  - 只读：本模块不写任何表。
"""
from __future__ import annotations

import logging
from typing import Any

from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.Dossier")

_DOSSIER_FACT_LIMIT = 50  # 画像/学到两章各取前 N 条（按 trust_score×新鲜度排）


def _tenant_clause(user_id: str, bank_id: str) -> tuple[str, list[str]]:
    from ducky.scope_sql import scope_clause
    from ducky.bank_contract import make_scope
    return scope_clause(make_scope(user_id, bank_id), flavor="canonical")


def build_dossier_data(user_id: str, bank_id: str) -> dict[str, Any]:
    """聚合域内数据（唯一接触数据库的函数）。"""
    data: dict[str, Any] = {"user_id": user_id, "bank_id": bank_id, "sections": {}}
    frag, params = _tenant_clause(user_id, bank_id)
    conn = get_facts_conn()
    try:
        # ── facts 总览 + epistemic 分布 ──
        total = conn.execute(
            f"SELECT COUNT(*) FROM facts WHERE archived=0 {frag}", params).fetchone()[0]
        archived = conn.execute(
            f"SELECT COUNT(*) FROM facts WHERE archived=1 {frag}", params).fetchone()[0]
        epi_rows = conn.execute(
            f"SELECT COALESCE(epistemic_mode,'fuzzy') m, COUNT(*) c FROM facts "
            f"WHERE archived=0 {frag} GROUP BY m", params).fetchall()
        epistemic_dist = {m: c for m, c in epi_rows}
        cat_rows = conn.execute(
            f"SELECT category, COUNT(*) c FROM facts WHERE archived=0 {frag} "
            f"GROUP BY category ORDER BY c DESC LIMIT 8", params).fetchall()

        # ── 演化与候选（v21 新表；演化表为全局平表——口径如实拆报）──
        try:
            evolution_global = conn.execute(
                "SELECT COUNT(*) FROM knowledge_evolution").fetchone()[0]
        except Exception:
            evolution_global = 0
        # 域内可关联：经 memory_types 的域内键收窄（覆盖受类型账本登记率限制，
        # 如实报告，不装全）
        try:
            evolution_domain = conn.execute(
                "SELECT COUNT(*) FROM knowledge_evolution ke WHERE EXISTS ("
                "  SELECT 1 FROM memory_types mt WHERE mt.memory_ref=ke.source_id"
                f"  AND 1=1 {frag})",
                params).fetchone()[0]
        except Exception:
            evolution_domain = None
        # mem0 主链路腿已打标数（v21.0 sidecar，缺表如实 0）
        try:
            sidecar_rows = conn.execute(
                f"SELECT epistemic_mode, COUNT(*) FROM memory_epistemic "
                f"WHERE 1=1 {frag} GROUP BY epistemic_mode",
                params).fetchall()
        except Exception:
            sidecar_rows = []
        try:
            candidates = conn.execute(
                f"SELECT status, COUNT(*) c FROM reflection_candidates "
                f"WHERE 1=1 {frag} GROUP BY status", params).fetchall()
        except Exception:
            candidates = []

        data["sections"]["health"] = {
            "facts_active": total,
            "facts_archived": archived,
            "epistemic_dist": epistemic_dist,
            "top_categories": cat_rows,
            "evolution_rows": evolution_global,
            "evolution_domain_linkable": evolution_domain,
            "sidecar_dist": {m: c for m, c in sidecar_rows},
            "reflection_candidates": {s: c for s, c in candidates},
        }

        # ── 用户画像（user_provided）与 AI 学到的（reasoned）──
        def _facts_by_mode(mode: str) -> list[tuple]:
            return conn.execute(
                f"SELECT category, fact_key, fact_value, trust_score, updated_at "
                f"FROM facts WHERE archived=0 AND epistemic_mode=? {frag} "
                f"ORDER BY trust_score DESC, updated_at DESC LIMIT ?",
                [mode, *params, _DOSSIER_FACT_LIMIT]).fetchall()

        data["sections"]["profile"] = _facts_by_mode("user_provided")
        data["sections"]["learned"] = _facts_by_mode("reasoned")

        # ── 立场观点（opinions 只有 owner 轴，无 bank 列——如实标注）──
        try:
            op_rows = conn.execute(
                "SELECT o.stance, o.confidence, f.fact_key, f.fact_value "
                "FROM opinions o JOIN facts f ON f.id=o.fact_id "
                "WHERE o.owner=? ORDER BY o.confidence DESC LIMIT 30",
                (user_id,)).fetchall()
        except Exception:
            op_rows = []
        data["sections"]["opinions"] = op_rows

        # ── 场景聚类 ──
        try:
            scene_rows = conn.execute(
                f"SELECT category, summary, member_count, created_at FROM scenes "
                f"WHERE 1=1 {frag} ORDER BY member_count DESC LIMIT 20",
                params).fetchall()
        except Exception:
            scene_rows = []
        data["sections"]["scenes"] = scene_rows

        # ── 技能结晶（全局表：技能非私密记忆，经人工 crystals_approve 闸门）──
        try:
            crystal_rows = conn.execute(
                "SELECT skill_name, trigger_rule, use_count, success_count, status "
                "FROM skill_crystals WHERE status IN ('approved','candidate') "
                "ORDER BY use_count DESC LIMIT 20").fetchall()
        except Exception:
            crystal_rows = []
        data["sections"]["crystals"] = crystal_rows
    finally:
        conn.close()

    # ── 检索进化（独立 evolve 库）──
    try:
        from ducky.evolve_mem import get_evolve_conn
        econn = get_evolve_conn()
        try:
            def _count(table: str) -> int:
                try:
                    return econn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except Exception:
                    return 0
            data["sections"]["evolve"] = {
                "queries": _count("evolve_queries"),
                "feedback": _count("evolve_feedback"),
                "adjustments": _count("evolve_adjustments"),
            }
        finally:
            econn.close()
    except Exception as e:
        logger.debug("dossier evolve 段跳过: %s", e)
        data["sections"]["evolve"] = {}

    # v21.2 M8：当前生效的跨殿借阅（谁能看我的记忆，一目了然）
    try:
        from ducky.pantheon import list_hall_grants
        granted = [g for g in list_hall_grants(user_id, direction="granted")
                   if not g.get("revoked_at")]
        received = [g for g in list_hall_grants(user_id, direction="received")
                    if not g.get("revoked_at")]
        data["sections"]["grants"] = {"granted": granted, "received": received}
    except Exception as e:
        logger.debug("dossier grants 段跳过: %s", e)
        data["sections"]["grants"] = {"granted": [], "received": []}

    return data


def render_markdown(data: dict[str, Any]) -> str:
    """纯渲染：data → Markdown。零数据库依赖，可独立测试。"""
    u, b = data.get("user_id", ""), data.get("bank_id", "")
    s = data.get("sections", {})
    out: list[str] = []
    out.append("# 记忆档案（Memory Dossier）")
    out.append("")
    out.append(f"> 域：`{u}` / `{b}` · 由 aiduMEI v21 导出 · "
               "user_provided=用户亲口事实 · reasoned=AI 推断（未经验证）")
    out.append("")

    h = s.get("health", {})
    out.append("## 一、记忆健康总览")
    out.append("")
    out.append(f"- 活跃事实：**{h.get('facts_active', 0)}** 条（已归档 {h.get('facts_archived', 0)} 条）")
    dist = h.get("epistemic_dist") or {}
    if dist:
        parts = " · ".join(f"{k} {v}" for k, v in sorted(dist.items()))
        out.append(f"- 出身分布：{parts}")
    cats = h.get("top_categories") or []
    if cats:
        out.append("- 热点类目：" + " · ".join(f"{c}({n})" for c, n in cats))
    out.append(f"- 演化关系记录：全局 {h.get('evolution_rows', 0)} 条"
               + (f"（其中本域可关联 {h['evolution_domain_linkable']} 条）"
                  if h.get("evolution_domain_linkable") is not None else "")
               + " —— 口径：演化表为全局平表（v20 设计决策），不按域收窄")
    sc = h.get("sidecar_dist") or {}
    if sc:
        out.append("- 主链路已打标（mem0 腿 sidecar）："
                   + " · ".join(f"{k} {v}" for k, v in sorted(sc.items())))
    rc = h.get("reflection_candidates") or {}
    if rc:
        out.append("- 反思候选：" + " · ".join(f"{k} {v}" for k, v in sorted(rc.items())))
    out.append("")

    out.append("## 二、用户画像（user_provided · 亲口事实）")
    out.append("")
    for cat, key, val, trust, _upd in s.get("profile") or []:
        out.append(f"- **[{cat}] {key}**：{val}（信任 {trust}）")
    if not s.get("profile"):
        out.append("- （暂无）")
    out.append("")

    out.append("## 三、AI 自己学到的（reasoned · ⚠️ 未经验证）")
    out.append("")
    for cat, key, val, trust, _upd in s.get("learned") or []:
        out.append(f"- **[{cat}] {key}**：{val}（信任 {trust} · 未经验证）")
    if not s.get("learned"):
        out.append("- （暂无）")
    out.append("")

    out.append("## 四、立场与观点")
    out.append("")
    out.append("> 口径：opinions 表仅 owner 轴（无 bank 列），本章按 user 收窄。")
    for stance, conf, key, val in s.get("opinions") or []:
        out.append(f"- **{stance}**（{conf:.2f}）· {key}：{str(val)[:80]}")
    if not s.get("opinions"):
        out.append("- （暂无）")
    out.append("")

    out.append("## 五、场景聚类")
    out.append("")
    for cat, summary, cnt, _ct in s.get("scenes") or []:
        out.append(f"- **[{cat or '未分类'}]**（{cnt} 条）{summary}")
    if not s.get("scenes"):
        out.append("- （暂无）")
    out.append("")

    out.append("## 六、技能结晶")
    out.append("")
    out.append("> 口径：skill_crystals 为全局表（技能经人工审批，非私密记忆）。")
    crystals = s.get("crystals") or []
    for name, trig, use, succ, status in crystals:
        out.append(f"- **{name}**（{status} · 复用 {use} 次 / 成功 {succ} 次）：{trig}")
    if not crystals:
        out.append("- （暂无）")
    elif all(status == "candidate" and use == 0 for _n, _t, use, _s, status in crystals):
        # v21.0 收口（生产用户 🟢-3）：全是候选且零复用时如实提示审批闸门未过
        out.append("")
        out.append("> ⚠️ 所有结晶仍是候选且零复用——需运行 /crystals/detect 后经人工 "
                   "crystals_approve 审批才会生效。")
    out.append("")

    ev = s.get("evolve") or {}
    out.append("## 七、检索进化")
    out.append("")
    out.append(f"- 检索日志 {ev.get('queries', 0)} 条 · 用户反馈 {ev.get('feedback', 0)} 条 · "
               f"调整动作 {ev.get('adjustments', 0)} 次")
    out.append("")

    # v21.2 M8：当前生效借阅 —— 「谁能看我的记忆」必须能一眼看到
    gr = s.get("grants") or {}
    _granted = gr.get("granted") or []
    _received = gr.get("received") or []
    out.append("## 八、当前生效借阅")
    out.append("")
    if not _granted and not _received:
        out.append("- 无（没有任何殿能读这座殿的记忆，这座殿也没借阅别处）")
    else:
        out.append(f"- 我授权出去 {len(_granted)} 条 · 我获授权 {len(_received)} 条")
        for g in _granted[:20]:
            out.append(f"  - → `{g.get('grantee_user_id', '')}` 可 {g.get('actions', '')}"
                       f"（库 {g.get('bank_id', '*')}，到期 {g.get('expires_at') or '不限'}）")
        for g in _received[:20]:
            out.append(f"  - ← 来自 `{g.get('grantor_user_id', '')}`：{g.get('actions', '')}"
                       f"（库 {g.get('bank_id', '*')}，到期 {g.get('expires_at') or '不限'}）")
    out.append("")

    return "\n".join(out)
