"""从facts.db恢复所有活跃记忆到Qdrant（on_disk模式）"""
import os
import sys, json, requests


_REPO = os.environ.get("AIDUMEM_HOME") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
os.chdir(_REPO)


# ── f0.6 W-22（cue 审计 A08）：遗留恢复工具默认拒绝执行 ─────────────────
# 本脚本按 user_id 硬编码 default、不保留 bank 归属：具名 bank 的内容会被
# 写进 default 域（跨域污染），且按 HTTP 200 计成功、无幂等键。正式恢复
# 入口是 docs/BACKUP_RESTORE.md 的 restore_gate 隔离流程。确要冒险执行
# （如单主人 default-only 库的应急回灌），必须显式设置环境变量声明知情。
if os.environ.get("AIDUMEI_LEGACY_RESTORE_ACK") != "yes-this-script-loses-bank-scope":
    print("[W-22] 拒绝执行：本遗留恢复脚本会把所有内容写进 default 域、丢失 bank 归属。")
    print("       正式恢复请走 docs/BACKUP_RESTORE.md 的 restore_gate 流程。")
    print("       单主人 default-only 库应急执行：AIDUMEI_LEGACY_RESTORE_ACK=yes-this-script-loses-bank-scope")
    sys.exit(2)


from ducky.utils import get_facts_conn

# 🔴P0-1（v19.4.1）：凭据从 ducky.utils 统一取（环境变量 → .env 兜底）。
# cron 不会加载 .env，若各脚本各自读环境变量，门禁一开就会集体静默 401。
from ducky.utils import api_auth_headers as _auth_headers  # noqa: E402


# 1. 从facts.db读
conn = get_facts_conn()
rows = conn.execute(
    "SELECT id, fact_key, fact_value, category, summary FROM facts WHERE archived=0 ORDER BY id"
).fetchall()
conn.close()
print(f"facts.db 活跃记忆: {len(rows)} 条")

# 2. 通过API逐条add
api = 'http://127.0.0.1:8767'
success = fail = 0

for i, row in enumerate(rows):
    mem_id, key, value, category, summary = row
    content = f"{key}: {value}"
    if summary:
        content += f" ({summary})"

    # messages必须是JSON字符串
    messages = json.dumps([{'role': 'user', 'content': content}])

    body = {
        'messages': messages,
        'user_id': 'default',
        'caller_user_id': 'default',
        'metadata': {
            'fact_id': mem_id,
            'category': category,
            'fact_key': key,
        },
    }

    try:
        resp = requests.post(f'{api}/add', json=body, timeout=30, headers=_auth_headers())
        if resp.status_code == 200:
            success += 1
        else:
            fail += 1
            if fail <= 3:
                print(f"  X [{resp.status_code}] id={mem_id}: {resp.text[:100]}")
    except Exception as e:
        fail += 1
        if fail <= 3:
            print(f"  X id={mem_id}: {e}")

    if (i+1) % 100 == 0:
        print(f"  进度: {i+1}/{len(rows)}")

print(f"\n完成: 成功 {success}, 失败 {fail}")
