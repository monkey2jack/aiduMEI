"""f0.7 P0 HTTP 端到端回归(外审 sesame F-01/F-03/F-04 的复现脚本收编)。

每个内核在独立子进程里起进程内 app(api_server 导入期读环境变量,不能与其它用例共进程)。
"""
import json
import os
import subprocess
import sys

import pytest

_PROBE = os.path.join(os.path.dirname(__file__), "_f07_http_probe.py")


def _run(kernel: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AIDUMEM_", "AIDUMEI_"))}
    p = subprocess.run([sys.executable, _PROBE, kernel], capture_output=True, text=True, timeout=300, env=env)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("kernel", ["native", "mem0"])
def test_delete_erases_everywhere_but_restorable_tombstone(kernel):
    out = _run(kernel)
    assert out["facts_before"] is True                      # 负向对照:删前 /api/facts 确实读得到
    assert out["delete_status"] == "committed"
    assert out["search_after"] is False and out["facts_after"] is False
    assert out["residue"] == {}                              # 墓碑(TTL)与已申报的 evolve_queries 之外零残留
    assert out["update_status"] == 200 and out["facts_new_value"] and not out["facts_old_value"]
    if kernel == "native":
        assert out["bob_update"] == 200 and out["bob_delete"] == "committed"
        assert out["persona_rows"] == [["alice", "alice 的人设:医生,住在杭州"]]   # bob 只动了自己的行
