"""
tests/test_v19_4_2_identity_env_fallback.py — 身份必须和凭据走同一条 .env 兜底链

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么有这个文件
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v19.4.2 修的是「代码里写了 Bearer，实际每次都发空 token」——
根因是网关/编辑器/cron 拉起进程时**环境几乎是空的**，而凭据只认环境变量。
修法是给凭据加一条 `.env` 兜底链。

修完之后，链只搬运了**一个键**：`AIDUMEM_API_TOKEN`。
身份（`AIDUMEM_USER_ID` / `AIDUMEM_DEFAULT_USER_ID`）仍然只认环境变量。
于是在同一个空环境里，结果变成：

    token 读到了  ✅   身份读不到，回落 `default`  ❌

请求**带着合法凭据打到错误的租户**。这比 401 更坏：

  · 401 会吵 —— 有 `--selftest`、有 stderr 诊断、有插件的 warning；
  · 租户错了**完全安静** —— HTTP 200、结果为空、日志干净，
    表现就是「记忆明明存了，就是搜不到」，和 v19.4.1 那一周一模一样。

这就是本版反复在讲的那件事的又一次现形：**守卫的射程小于缺陷的分布**。
兜底链是对的，射程只覆盖了两个必需值里的一个。

所以本文件的断言不是「某个文件里有没有写 DEFAULT_USER_ID」，
而是**真的把进程放进空环境里跑一遍**，看它最终按谁的身份发请求。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
射程锁定
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
`test_every_integration_entry_point_is_covered` 把「集成件入口的集合」
本身写成断言：扫 `integrations/` 下所有会带 user_id 发请求的可执行件，
要求每一个都在本文件里有对应的行为用例。
将来新增一个集成件而忘了接兜底链，是这条测试先红，而不是用户先丢记忆。
"""

import json
import os
import pathlib
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INTEGRATIONS = _REPO_ROOT / "integrations"

# .env 里只写身份，不写 token —— 单独考身份这一条链有没有通。
_ENV_USER = "tenant-from-env-file"


# ────────────────────────────────────────────────────────────────
# 夹具
# ────────────────────────────────────────────────────────────────

@pytest.fixture()
def env_file(tmp_path):
    """一份和生产同形态的 .env：export 前缀 + 引号 + CRLF + 注释。

    这三种写法都是真实部署里出现过的。解析器不认其中任何一种，
    症状都是「静默读不到」，与「根本没配」无法区分。
    """
    p = tmp_path / "deploy" / ".env"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(
        b"# aiduMEI deployment config\r\n"
        b"export AIDUMEM_API_TOKEN=\"tok-for-test\"\r\n"
        b"AIDUMEM_DEFAULT_USER_ID='" + _ENV_USER.encode() + b"'\r\n"
    )
    return p


def _empty_env(env_file_path, tmp_path, **extra):
    """近乎空的环境 —— 网关/编辑器/cron 拉起子进程时的真实样子。

    只保留 PATH（否则连 python3 都找不到）与一个不存在的 HOME
    （防止误读到开发机自己的 ~/.aidumem/.env 而假绿灯）。
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "nohome"),
        "AIDUMEM_ENV_FILE": str(env_file_path),
    }
    env.update(extra)
    return env


class _CaptureHandler(BaseHTTPRequestHandler):
    captured: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).captured.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "user_id": body.get("user_id"),
        })
        payload = b'{"results": [], "status": "ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture()
def stub_server():
    """本地打桩服务，记录 hook 实际发出的 user_id 与 Authorization。

    断言看的是**线上真正发出去的那个值**，不是源码里读到的字符串 ——
    这正是 v19.4.1「代码里明明写了」却全线 401 的教训。
    """
    handler = type("H", (_CaptureHandler,), {"captured": []})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", handler
    srv.shutdown()
    srv.server_close()


# ────────────────────────────────────────────────────────────────
# 行为用例：四个集成件入口 + 仓库内单一真源
# ────────────────────────────────────────────────────────────────

def test_inject_hook_resolves_identity_from_env_file(env_file, tmp_path, stub_server):
    """`aidumem-inject.sh`（Hermes pre_llm_call）—— 生产上真正注册的那个。"""
    url, handler = stub_server
    hook = _INTEGRATIONS / "aidumem-inject.sh"
    proc = subprocess.run(
        ["bash", str(hook), "--selftest"],
        env=_empty_env(env_file, tmp_path, AIDUMEM_URL=url),
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"selftest 未通过：{proc.stderr}"
    assert handler.captured, "hook 没有发出任何请求"
    sent = handler.captured[-1]
    assert sent["user_id"] == _ENV_USER, (
        f"hook 在空环境下按 {sent['user_id']!r} 检索，而 .env 写的是 {_ENV_USER!r}。"
        "凭据读到了、身份没读到 —— 请求会带着合法 token 打到错误租户，"
        "而且 HTTP 200、日志干净，没有任何一处会报错。"
    )
    assert sent["auth"] == "Bearer tok-for-test", "凭据链回归了"


def test_inject_hook_selftest_always_reports_identity(env_file, tmp_path):
    """自检输出**每一条**都要带 user_id，成功失败都带。

    身份错和 token 错的表象都是「记忆搜不到」。诊断行若不先回答
    「这次是按谁的身份问的」，排查会一路跑偏到网络和 token 上。
    """
    hook = _INTEGRATIONS / "aidumem-inject.sh"
    proc = subprocess.run(
        ["bash", str(hook), "--selftest"],
        # 9 端口必不可达 —— 强制走 unreachable 分支
        env=_empty_env(env_file, tmp_path, AIDUMEM_URL="http://127.0.0.1:9"),
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 4, f"预期 unreachable(4)，实得 {proc.returncode}"
    assert f"user_id={_ENV_USER}" in proc.stderr, (
        f"连不上时的诊断行没有报出身份，实际输出：{proc.stderr!r}"
    )


def test_on_save_hook_resolves_identity_from_env_file(env_file, tmp_path, stub_server):
    """`aidumem-on-save.sh`（编辑器保存钩子）—— 写入侧。

    写入侧搞错租户比读取侧更难发现：写成功了，只是落在别人的分区里。
    """
    url, handler = stub_server
    src = tmp_path / "sample.py"
    src.write_text("print('hello')\n", encoding="utf-8")
    hook = _INTEGRATIONS / "cursor-hook" / "aidumem-on-save.sh"
    proc = subprocess.run(
        ["bash", str(hook), str(src), "test"],
        env=_empty_env(env_file, tmp_path, AIDUMEM_URL=url),
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"on-save 退出码 {proc.returncode}：{proc.stderr}"
    assert handler.captured, "on-save 没有发出任何请求"
    sent = handler.captured[-1]
    assert sent["user_id"] == _ENV_USER, (
        f"on-save 在空环境下把记忆写进 {sent['user_id']!r} 分区，"
        f".env 写的是 {_ENV_USER!r} —— 写成功了，用户在自己分区里永远搜不到。"
    )
    assert sent["auth"] == "Bearer tok-for-test", "凭据链回归了"


def test_claude_code_hook_resolves_identity_from_env_file(env_file, tmp_path):
    """`claude-code-hook.py` —— 含「拷出仓库独立运行」的兜底分支。"""
    hook = _INTEGRATIONS / "cursor-hook" / "claude-code-hook.py"
    code = (
        "import runpy, sys; "
        f"m = runpy.run_path({str(hook)!r}); "
        "print(m['AIDUMEM_USER_ID'])"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=_empty_env(env_file, tmp_path),
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == _ENV_USER, (
        f"claude-code-hook 解析出的身份是 {proc.stdout.strip()!r}，"
        f"而 .env 写的是 {_ENV_USER!r}"
    )


def test_hermes_plugin_resolves_identity_from_env_file(env_file, tmp_path):
    """`hermes-plugin` —— 跑在宿主进程里，不能 import 仓库代码，自带一份实现。

    「自带一份」是允许的，前提是它实现的**是同一条链**。
    """
    plugin_dir = _INTEGRATIONS / "hermes-plugin"
    # 插件在 import 期就要 `from agent.memory_provider import MemoryProvider`
    # ——那是宿主 Agent 的包，仓库里没有。打桩宿主，这样跑的仍是插件真实源码。
    code = (
        "import sys, types\n"
        "for name, attrs in (('agent', {}), ('agent.memory_provider', "
        "{'MemoryProvider': type('MemoryProvider', (), {})}), "
        "('tools', {}), ('tools.registry', "
        "{'tool_error': lambda *a, **k: None})):\n"
        "    mod = types.ModuleType(name)\n"
        "    mod.__dict__.update(attrs)\n"
        "    sys.modules[name] = mod\n"
        "sys.path.insert(0, %r)\n"
        "import aidumem\n"
        "print(aidumem._resolve_user_id())\n" % str(plugin_dir)
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=_empty_env(env_file, tmp_path),
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == _ENV_USER, (
        f"hermes 插件解析出的身份是 {proc.stdout.strip()!r}，"
        f"而 .env 写的是 {_ENV_USER!r}"
    )


def test_ducky_utils_default_user_id_reads_env_file(env_file, tmp_path):
    """仓库内的单一真源 `ducky.utils.DEFAULT_USER_ID`。

    它被用作 128 处函数默认参数，还被写进建表 DDL 的 DEFAULT 子句，
    因此必须在 import 期就取到正确值 —— 晚绑定改不动它。
    受影响最直接的是 mem0_sync.py（`USER_ID = DEFAULT_USER_ID`），
    它的 systemd 单元历史上没有 EnvironmentFile，环境就是空的。
    """
    code = "from ducky.utils import DEFAULT_USER_ID; print(DEFAULT_USER_ID)"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_REPO_ROOT),
        env=_empty_env(env_file, tmp_path),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == _ENV_USER, (
        f"DEFAULT_USER_ID 为 {proc.stdout.strip()!r}，未从 .env 兜底读到 {_ENV_USER!r}"
    )


def test_explicit_env_var_still_outranks_env_file(env_file, tmp_path):
    """优先级不能反：显式环境变量永远压过 .env。

    否则「临时换个身份跑一次」这种最常见的操作会静默失效。
    """
    code = "from ducky.utils import DEFAULT_USER_ID; print(DEFAULT_USER_ID)"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_REPO_ROOT),
        env=_empty_env(env_file, tmp_path, AIDUMEM_DEFAULT_USER_ID="explicit-wins"),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "explicit-wins", (
        f"环境变量没能压过 .env，实得 {proc.stdout.strip()!r}"
    )


def test_env_file_parser_is_not_duplicated_in_repo_code():
    """仓库内只允许有**一个** .env 解析器。

    独立集成件（integrations/）因为不能 import 仓库代码，各自带一份是允许的；
    但仓库内部再长出第二个解析器，就会重演「这个组件认 export 前缀、
    那个不认」的分裂 —— 而分裂的症状是静默读不到值。
    """
    utils = (_REPO_ROOT / "ducky" / "utils.py").read_text(encoding="utf-8")
    assert utils.count("def parse_env_file") == 1
    assert "def load_env_file" in utils and "parse_env_file(path)" in utils, (
        "load_env_file 必须复用 parse_env_file，不得自带第二份解析逻辑"
    )


# ────────────────────────────────────────────────────────────────
# 改名的连带面：历史数据会不会因为身份改名而失明
# ────────────────────────────────────────────────────────────────
#
# 上面几条守的是「身份解析对不对」。但身份解析**对了**之后还有第二跳：
# 改名之前已经落库的数据，是按旧名字（字面量 'default'）存的。
# reflections 的读取是 `WHERE user_id=?` 精确匹配，没有通配分支 ——
# 于是改名当天，历史洞察对服务**彻底失明**：不报错、不告警，查出来 0 条。
#
# 生产实测：2026-08-19 身份改名，让 8-17 写下的 10 条真反思一起消失，
# 靠逐库清点 user_id 分布才捞回来。本组用例把这一跳钉死。

_LEGACY_CONTENT = "改名之前落库的历史洞察"
_NAMED_CONTENT = "具名租户 alice 自己的洞察"

# 在子进程里跑：DEFAULT_USER_ID 是 import 期绑定的，改环境变量之后
# 必须重新起进程才作数 —— 在同一进程里 monkeypatch 只会测出假象。
_REFLECT_PROBE = '''
import json
from ducky import reflect
from ducky.utils import DEFAULT_USER_ID, get_facts_conn

reflect.ensure_reflect_schema()
conn = get_facts_conn()
for uid, content in (("default", %r), ("alice", %r)):
    conn.execute(
        "INSERT INTO reflections (user_id, insight_type, content, confidence,"
        " evidence, source, recorded_at) VALUES (?,?,?,?,?,?,?)",
        (uid, "pattern", content, 0.9, "[]", "seed", "2026-08-17T21:06:55+00:00"),
    )
conn.commit()
conn.close()

print(json.dumps({
    "default_user_id": DEFAULT_USER_ID,
    "seen_by_default_identity": sorted(
        r["content"] for r in reflect.get_reflections(DEFAULT_USER_ID, limit=50)),
    "seen_by_alice": sorted(
        r["content"] for r in reflect.get_reflections("alice", limit=50)),
    "dup_added": reflect.save_insights(
        [{"content": %r, "type": "pattern"}], DEFAULT_USER_ID, "seed"),
}, ensure_ascii=False))
''' % (_LEGACY_CONTENT, _NAMED_CONTENT, _LEGACY_CONTENT)


def _run_reflect_probe(env_file_path, tmp_path, **extra):
    data_dir = tmp_path / ("data-" + str(len(extra)) + str(abs(hash(str(extra))) % 9973))
    data_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-c", _REFLECT_PROBE],
        cwd=str(_REPO_ROOT),
        env=_empty_env(env_file_path, tmp_path,
                       AIDUMEM_DATA_DIR=str(data_dir), **extra),
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"探针失败：{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_reflections_written_before_rename_stay_visible(env_file, tmp_path):
    """身份改名之后，改名前写下的洞察必须仍然看得见。

    这条红 = 用户的历史反思在一次配置变更后静默消失，而系统一切正常。
    """
    out = _run_reflect_probe(env_file, tmp_path)
    assert out["default_user_id"] == _ENV_USER, "前置条件没成立：身份没从 .env 读到"
    assert _LEGACY_CONTENT in out["seen_by_default_identity"], (
        f"默认身份改名为 {_ENV_USER!r} 之后，改名前以字面量 'default' 落库的洞察"
        f"查不到了（实得 {out['seen_by_default_identity']!r}）。"
        "表现是 0 条、无报错、无日志 —— 用户只会觉得「反思怎么没了」。"
    )


def test_reflections_widening_does_not_leak_named_tenants(env_file, tmp_path):
    """放宽只针对字面量 'default'，绝不能顺手把具名租户也捞进来。"""
    out = _run_reflect_probe(env_file, tmp_path)
    assert _NAMED_CONTENT not in out["seen_by_default_identity"], (
        "默认身份看到了具名租户 alice 的洞察 —— 放宽写过头了，这是越权"
    )
    assert out["seen_by_alice"] == [_NAMED_CONTENT], (
        f"具名租户的可见范围被改动了，实得 {out['seen_by_alice']!r}；"
        "alice 既不该看到 'default' 的历史条目，也不该看到别人的"
    )


def test_reflections_widening_does_not_create_duplicates(env_file, tmp_path):
    """读取放宽之后，去重也必须跟着放宽，否则同一条洞察会被写第二遍。

    症状很隐蔽：放宽读取本是为了「让它可见」，结果一次 reflect 就把它
    在新身份名下再写一份，然后两条一起显示 —— 修复反而制造了重复。
    """
    out = _run_reflect_probe(env_file, tmp_path)
    assert out["dup_added"] == 0, (
        f"已存在于 'default' 名下的洞察又被写了 {out['dup_added']} 条到 "
        f"{_ENV_USER!r} 名下 —— 去重没有跟着读取一起放宽"
    )
    assert out["seen_by_default_identity"].count(_LEGACY_CONTENT) == 1, (
        f"同一条洞察出现了多次：{out['seen_by_default_identity']!r}"
    )


def test_legacy_deployment_behaviour_is_byte_identical(env_file, tmp_path):
    """默认身份**就叫** default 的老部署，行为必须逐字节不变。

    「只加不减」的字面含义：没改名的部署，这次升级对它应当完全没有感知。
    """
    out = _run_reflect_probe(env_file, tmp_path, AIDUMEM_DEFAULT_USER_ID="default")
    assert out["default_user_id"] == "default", "前置条件没成立"
    assert out["seen_by_default_identity"] == [_LEGACY_CONTENT], (
        f"老部署的可见范围变了，实得 {out['seen_by_default_identity']!r}"
    )
    assert out["seen_by_alice"] == [_NAMED_CONTENT]
    assert out["dup_added"] == 0


# ────────────────────────────────────────────────────────────────
# 射程锁定：集合本身就是断言
# ────────────────────────────────────────────────────────────────

# 已被上面行为用例覆盖的入口（相对 integrations/ 的路径）
_COVERED = {
    "aidumem-inject.sh",
    "cursor-hook/aidumem-on-save.sh",
    "cursor-hook/claude-code-hook.py",
    "hermes-plugin/aidumem/__init__.py",
}

_BACKUP = re.compile(r"\.bak(-|\.|$)|~$")


def _entry_points_that_send_user_id():
    """扫出所有「会带 user_id 发请求」的集成件可执行文件。

    判据是源码里同时出现 user_id 与 HTTP 调用痕迹，而不是一张手写清单 ——
    手写清单正是上一轮漏掉四个入口的原因。
    """
    found = set()
    for path in sorted(_INTEGRATIONS.rglob("*")):
        if not path.is_file() or path.suffix not in (".sh", ".py"):
            continue
        if _BACKUP.search(path.name) or "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "user_id" not in text:
            continue
        if not any(k in text for k in ("curl", "urllib", "requests", "http")):
            continue
        found.add(str(path.relative_to(_INTEGRATIONS)))
    return found


def test_every_integration_entry_point_is_covered():
    """新增集成件必须同时补一条行为用例 —— 否则这条先红。

    这就是本版的核心教训写成可执行形式：守卫的射程必须覆盖缺陷的分布。
    上一轮凭据修复计划点名 5 个入口、实际有 9 个，靠的是人肉找齐；
    这条断言让「找齐」变成机器的事。
    """
    found = _entry_points_that_send_user_id()
    assert found, "扫描范围写错了 —— 一个集成件入口都没找到，这条守卫等于没开"
    missing = found - _COVERED
    assert not missing, (
        "以下集成件会带 user_id 发请求，但本文件没有对应的行为用例：\n  "
        + "\n  ".join(sorted(missing))
        + "\n请为它补一条「空环境 + .env 兜底」用例，再把路径加进 _COVERED。"
    )
    stale = _COVERED - found
    assert not stale, f"_COVERED 里有已不存在的入口：{sorted(stale)}"
