"""后台恢复：从facts.db写回Qdrant（慢速，每条约1.5秒）"""
import sys, os, json, requests, time

_REPO = os.environ.get("AIDUMEM_HOME") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
os.chdir(_REPO)

from ducky.utils import get_facts_conn

conn = get_facts_conn()
# 跳过已恢复的：从Qdrant已有的数量推算
rows = conn.execute(
    "SELECT id, fact_key, fact_value, category, summary FROM facts WHERE archived=0 ORDER BY id"
).fetchall()
conn.close()

# 快速check当前Qdrant有多少条了
resp = requests.post('http://127.0.0.1:8767/search', json={'query':'test','user_id':'default','limit':1})
already = 0  # 不好直接查，直接全量覆盖写

api = 'http://127.0.0.1:8767'
success = fail = 0
total = len(rows)
print(f'START total={total}', flush=True)

for i, row in enumerate(rows):
    mem_id, key, value, category, summary = row
    content = f"{key}: {value}"
    if summary:
        content += f" ({summary})"
    
    messages = json.dumps([{'role': 'user', 'content': content}])
    body = {'messages': messages, 'user_id': 'default', 'metadata': {'fact_id': mem_id, 'category': category, 'fact_key': key}}
    
    try:
        resp = requests.post(f'{api}/add', json=body, timeout=30)
        if resp.status_code == 200:
            success += 1
        else:
            fail += 1
    except Exception:
        fail += 1
    
    if (i+1) % 200 == 0:
        print(f'PROGRESS {i+1}/{total} ok={success} fail={fail}', flush=True)
    time.sleep(0.1)  # 别打太猛

print(f'DONE total={total} ok={success} fail={fail}', flush=True)
