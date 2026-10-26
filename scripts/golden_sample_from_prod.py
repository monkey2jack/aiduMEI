#!/usr/bin/env python3
"""f0.6++ WP7 · 黄金集真语料取样(生产宿主域只读)。

五类×40 条映射:
  fact_recall  <- pattern_kv / pattern_version (单点事实)
  temporal     <- pattern_datetime / ops (时间敏感)
  preference   <- pattern_preference / 重要记忆 (偏好与铁律)
  negation     <- 全域搜「不是/没有/从未/删了/不再」 特征事实
  multi_hop    <- Experience / Solution (跨条拼接题的原料)

每类输出 40 条候选 JSONL(预期 id 留空,由人工/自动复核后回填)。
只读:SELECT only;输出到 stdout 管道落 /tmp,不写生产任何文件。
"""
import sqlite3
import json
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "data/facts.db"
PER = 40

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row


def rows(sql, args=()):
    return [dict(r) for r in conn.execute(sql, args)]


def fact_row(r, category, q):
    return {
        "category": category,
        "source_fact_id": r["id"],
        "user_id": r.get("user_id", "prod_owner"),
        "bank_id": r.get("bank_id", "default"),
        "fact_text": (r.get("fact_value") or r.get("content") or "")[:300],
        "category_raw": r.get("category", ""),
        "created_at": str(r.get("valid_from") or r.get("created_at") or ""),
        "planned_query": q,
    }


out = []

# 1. fact_recall:KV 值查询
kv = rows("SELECT * FROM facts WHERE category='pattern_kv' AND length(fact_value)>4 ORDER BY id DESC LIMIT ?", (PER,))
for r in kv[:PER // 2]:
    out.append(fact_row(r, "fact_recall", f"{r.get('fact_key','')} 的值是什么"))
ver = rows("SELECT * FROM facts WHERE category='pattern_version' ORDER BY id DESC LIMIT ?", (PER,))
for r in ver[:PER // 2]:
    out.append(fact_row(r, "fact_recall", "对应的版本号是多少"))

# 2. temporal:pattern_datetime + ops(时间敏感)
dt = rows("SELECT * FROM facts WHERE category IN ('pattern_datetime','ops') ORDER BY id DESC LIMIT ?", (PER,))
for r in dt[:PER]:
    out.append(fact_row(r, "temporal", "这件事发生/记录在哪天"))

# 3. preference
pref = rows("SELECT * FROM facts WHERE category IN ('pattern_preference','重要记忆') ORDER BY id DESC LIMIT ?", (PER,))
for r in pref[:PER]:
    out.append(fact_row(r, "preference", "用户的偏好/要求是什么"))

# 4. negation:全 category 内搜否定线索
neg_kw = ["不是", "没有", "从未", "不再", "删了", "不要", "禁止", "别再"]
neg = []
for kw in neg_kw:
    neg += rows("SELECT * FROM facts WHERE fact_value LIKE ? ORDER BY id DESC LIMIT 20", (f"%{kw}%",))
seen = set()
for r in neg:
    if r["id"] in seen:
        continue
    seen.add(r["id"])
    out.append(fact_row(r, "negation", "关于这件事,哪些是被明确否定的"))
    if sum(1 for o in out if o["category"] == "negation") >= PER:
        break

# 5. multi_hop:Experience/Solution 长文(可拼接关系)
mh = rows("SELECT * FROM facts WHERE category IN ('Experience','Solution') AND length(fact_value)>80 ORDER BY id DESC LIMIT ?", (PER * 2,))
for r in mh[:PER]:
    out.append(fact_row(r, "multi_hop", "把这条经验与相关决策串起来说清楚"))

by_cat = {}
for o in out:
    by_cat[o["category"]] = by_cat.get(o["category"], 0) + 1
print(json.dumps({"total": len(out), "per_category": by_cat}, ensure_ascii=False))
sys.stdout = open("/tmp/golden_candidates.jsonl", "w", encoding="utf-8")
for o in out:
    print(json.dumps(o, ensure_ascii=False))
print(json.dumps({"total": len(out), "per_category": by_cat}, ensure_ascii=False), file=sys.stderr)
conn.close()
