"""从facts.db恢复所有活跃记忆到Qdrant（on_disk模式）"""
import os
import sys, os, json, requests, time


_REPO = os.environ.get("AIDUMEM_HOME") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
os.chdir(_REPO)

from ducky.utils import get_facts_conn

# 🔴P0-1（v19.4.1）：与后端读同一个环境变量携带 Bearer token。
# 后端启用鉴权门禁后不带凭据一律 401；本脚本属运维工具，
# 失败往往只体现为「没干活」，不补凭据会让配置错误长期潜伏。
def _auth_headers() -> dict:
    _token = os.environ.get("AIDUMEM_API_TOKEN", "").strip()
    return {"Authorization": f"Bearer {_token}"} if _token else {}


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
