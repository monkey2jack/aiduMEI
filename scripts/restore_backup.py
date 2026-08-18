import os
import sqlite3, json, requests, shutil, tempfile, os, sys, time

sys.path.insert(0, 'venv/lib/python3.12/site-packages')
from qdrant_client import QdrantClient

# 🔴P0-1（v19.4.1）：与后端读同一个环境变量携带 Bearer token。
# 后端启用鉴权门禁后不带凭据一律 401；本脚本属运维工具，
# 失败往往只体现为「没干活」，不补凭据会让配置错误长期潜伏。
def _auth_headers() -> dict:
    _token = os.environ.get("AIDUMEM_API_TOKEN", "").strip()
    return {"Authorization": f"Bearer {_token}"} if _token else {}


backup_path = 'data/qdrant/collection/mem0/storage.sqlite.bak.20260727_111301'

# 1. 复制备份到临时目录让QdrantClient读
tmpdir = tempfile.mkdtemp()
shutil.copytree('data/qdrant', tmpdir + '/qdrant', dirs_exist_ok=True)
os.makedirs(f'{tmpdir}/qdrant/collection/mem0', exist_ok=True)
shutil.copy(backup_path, f'{tmpdir}/qdrant/collection/mem0/storage.sqlite')

# 2. 读备份
backup_client = QdrantClient(path=f'{tmpdir}/qdrant')
all_points = []
offset = None
while True:
    result = backup_client.scroll(
        collection_name='mem0', limit=500, offset=offset,
        with_payload=True, with_vectors=True,
    )
    points, next_offset = result[0], result[1]
    all_points.extend(points)
    if next_offset is None:
        break
    offset = next_offset
print(f'备份读出: {len(all_points)} 条')

# 3. 写入
api_url = 'http://127.0.0.1:8767'
success = fail = 0
for i, pt in enumerate(all_points):
    if i % 200 == 0:
        print(f'  进度: {i}/{len(all_points)}')
    payload = pt.payload or {}
    body = {
        'messages': [{'role': 'user', 'content': payload.get('data', '')}],
        'user_id': payload.get('user_id', 'default'),
    }
    try:
        resp = requests.post(f'{api_url}/add', json=body, timeout=15, headers=_auth_headers())
        if resp.status_code == 200:
            success += 1
        else:
            fail += 1
            if fail <= 3:
                print(f'  X [{resp.status_code}]: {resp.text[:120]}')
    except Exception as e:
        fail += 1
        if fail <= 3:
            print(f'  X: {e}')

backup_client.close()
shutil.rmtree(tmpdir)
print(f'\n完成: 成功 {success}, 失败 {fail}')
