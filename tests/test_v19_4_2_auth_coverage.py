"""
tests/test_v19_4_2_auth_coverage.py — 守卫射程锁 + Hook 鉴权贯通

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么有这个文件（v19.4.2 的核心教训 · 反假绿灯铁律）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v19.4.1 带着**全绿的测试套件**发布了这些缺陷：

  · Hermes hook 每一次调用都 401 —— 记忆注入完全失效，且静默；
  · MEMORY.md 同步守护 401 + 崩溃循环 —— 静默停摆 8 天、重启 6000+ 次；
  · seed_demo / seed_facts —— 开源用户第一条命令就 401；
  · mcp_server —— 自带一份凭据读取，无 .env 兜底。

测试为什么没拦住？因为 v19.4.1 的鉴权守卫**只扫 `scripts/` 目录**，
而上述四个调用方一个都不在 `scripts/` 里。守卫的射程小于缺陷的分布 ——
它不是没开枪，是压根没瞄到那片区域。**这比没有守卫更危险**：
它提供了一种「已经防住了」的错觉。

所以本文件的重点不是再加几条断言，而是加一条**元断言**：

    守卫的覆盖集合  ⊇  全仓所有「以客户端身份调用本服务」的文件集合

也就是**用测试锁死守卫自己的射程**。今后任何人新增一个入口点
（新脚本、新 hook、新 integration），只要它发 HTTP 打本服务，
这条元测试就会立刻变红并点名 —— 它没法再悄悄溜过去。
"""

import os
import pathlib
import re
import subprocess
import sys
import textwrap

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_ROOT = pathlib.Path(_REPO_ROOT)

# 不扫描的目录：虚拟环境 / 版本库 / 运行期产物 / 前端资源 / 测试自身
_SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    "data", "data_mock", "logs", "frontend", "docs", "tests",
    ".pytest_cache", ".mypy_cache", "build", "dist",
}

# 服务端自身：它是门禁的**实施者**，不是通过门禁的人。
# 只因为源码里写了默认端口 8767 才会被下面的正则捞到，需显式豁免。
_SERVER_SIDE = {"api_server.py"}

# 「指向本服务」的信号
_TARGETS_SERVICE = re.compile(r"AIDUMEM_API_BASE|AIDUMEM_URL|127\.0\.0\.1:8767|localhost:8767|:8767")
# 「确实发起了 HTTP 请求」的信号（只提端口不算，比如文档字符串）
_MAKES_HTTP_PY = re.compile(r"requests\.(get|post|put|delete)\(|urllib\.request\.(urlopen|Request)\(|httpx\.")
_MAKES_HTTP_SH = re.compile(r"\bcurl\b|urllib\.request")
# 「带上了凭据」的信号
_CARRIES_AUTH = re.compile(r"api_auth_headers|_auth_headers|AUTH_ARGS|Authorization")


def _iter_repo_http_callers():
    """全仓扫描：返回所有「以客户端身份对本服务发 HTTP 请求」的文件。

    这是**事实集合** —— 仓库里客观存在多少个会撞上门禁的入口点。
    守卫的覆盖集合必须包住它。
    """
    callers = []
    for path in sorted(_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in (".py", ".sh"):
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(_ROOT).parts):
            continue
        if path.name in _SERVER_SIDE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not _TARGETS_SERVICE.search(text):
            continue
        pattern = _MAKES_HTTP_PY if path.suffix == ".py" else _MAKES_HTTP_SH
        if pattern.search(text):
            callers.append(path)
    return callers


def test_every_http_caller_carries_credentials():
    """全仓不变量：凡对本服务发 HTTP 的文件，都必须带凭据。

    与 v19.4.1 守卫的区别：那个按目录列举，这个按**行为**扫描全仓。
    """
    offenders = []
    for path in _iter_repo_http_callers():
        text = path.read_text(encoding="utf-8")
        if not _CARRIES_AUTH.search(text):
            offenders.append(str(path.relative_to(_ROOT)))
    assert not offenders, (
        "以下文件会对本服务发 HTTP 请求但不带任何凭据，门禁开启后必然静默 401："
        + ", ".join(offenders)
    )


def test_guard_coverage_is_not_narrower_than_reality():
    """★ 元测试：锁死守卫自己的射程 —— 本文件存在的根本理由。

    断言 v19.4.1 鉴权守卫实际检查的文件集合，覆盖全仓所有 HTTP 调用方。
    任何新增的入口点若落在守卫的扫描目录之外，这里立刻变红并点名，
    从根上杜绝「新入口点悄悄溜过 → 带着全绿测试发布缺陷」的重演。
    """
    import test_v19_4_1_auth_gate as guard

    # 守卫的覆盖集合：直接问守卫自己「你扫了哪些文件」，不在这里复刻它的 glob ——
    # 复刻会让两边各自漂移，元测试就变成了自说自话。
    covered = {p.resolve() for p in guard._iter_credential_consumers()}
    covered |= {p.resolve() for p in guard._iter_credential_consumer_shells()}

    reality = {p.resolve() for p in _iter_repo_http_callers()}

    blind_spots = sorted(str(p.relative_to(_ROOT)) for p in (reality - covered))
    assert not blind_spots, (
        "鉴权守卫存在盲区 —— 以下文件会调用本服务，却不在守卫的扫描范围内。\n"
        "这正是 v19.4.1 带着全绿测试发布 hook 401 / 同步停摆的原因。\n"
        "请把它们所在的目录并入 test_v19_4_1_auth_gate 的扫描面，而不是删掉本断言：\n  "
        + "\n  ".join(blind_spots)
    )


def test_guard_actually_sees_the_known_entry_points():
    """守卫射程的正面锚点：v19.4.1 漏掉的四个入口点，今天必须在覆盖集合里。

    上面的元测试是「集合关系」，理论上在两边都为空时也成立。
    这条钉死具体文件名，确保扫描器不会因为某次重构而空转成永真。
    """
    import test_v19_4_1_auth_gate as guard

    covered = {p.name for p in guard._iter_credential_consumers()}
    covered |= {p.name for p in guard._iter_credential_consumer_shells()}

    for name in ("mem0_sync.py", "seed_demo.py", "seed_facts.py",
                 "mcp_server.py", "aidumem-inject.sh",
                 "claude-code-hook.py", "aidumem-on-save.sh"):
        assert name in covered, f"{name} 脱离守卫覆盖 —— v19.4.1 的漏网之鱼又回来了"


def test_standalone_integrations_share_the_same_env_chain():
    """独立集成件（会被拷进宿主 Agent / 编辑器）必须实现同一条凭据兜底链。

    它们 import 不到 ducky，只能各自带一份实现 —— 但**行为**必须一致。
    否则就会出现最难排查的一类故障：同一份 .env，A 组件读得到、B 组件读不到，
    表现为「记忆有时候好使有时候不好使」，而实际上是配置分裂。
    """
    import test_v19_4_1_auth_gate as guard

    required = {
        "integrations/hermes-plugin/aidumem/__init__.py",
        "integrations/cursor-hook/claude-code-hook.py",
    }
    checked = set()
    for path in guard._iter_credential_consumers():
        rel = str(path.relative_to(_ROOT))
        if not guard._is_standalone_integration(path):
            continue
        checked.add(rel)
        assert guard._has_env_fallback_chain(path.read_text(encoding="utf-8")), (
            f"{rel} 的凭据兜底链不完整：只带 Authorization 头不够，"
            f"还必须能从 .env 真正取到 token —— v19.4.1 的 Hermes 插件就是"
            f"「写了 Bearer，但从空环境读出来是空串」，看着修了，实际全线 401"
        )
    missing = required - checked
    assert not missing, f"以下独立集成件脱离了守卫覆盖: {sorted(missing)}"


def test_hermes_plugin_surfaces_auth_failure():
    """Hermes 插件的 401 必须升到 warning，不能混在 debug 里。

    记忆层「失败不崩」是对的，但「失败不吭声」不是。
    401 是配置故障、不会自愈，埋在 debug 日志里等于永远没人发现 ——
    生产上记忆静默失效整整一天，就是这么来的。
    """
    text = (_ROOT / "integrations" / "hermes-plugin" / "aidumem" / "__init__.py").read_text(
        encoding="utf-8")
    assert "urlerror.HTTPError" in text, "插件未区分 HTTP 错误与网络错误"
    assert re.search(r"logger\.warning\(", text), "插件未对鉴权失败输出 warning"
    assert "_resolve_api_token" in text, "插件缺少 .env 兜底链"


# ═══════════════════════════════════════════════════════════════
# Hermes hook：鉴权与自检（P0-1 真凶的直接回归）
# ═══════════════════════════════════════════════════════════════

_HOOK = _ROOT / "integrations" / "aidumem-inject.sh"


def test_hook_sends_bearer_token():
    """hook 必须把 token 放进 Authorization 头。

    v19.4.1 的 hook 只发裸请求 —— 门禁开启后每次调用 401，
    而异常被 `except Exception: sys.exit(0)` 吞掉、stderr 又被 `2>/dev/null` 丢弃，
    于是「记忆注入完全失效」这件事在生产上安静地跑了一整天没人知道。
    """
    text = _HOOK.read_text(encoding="utf-8")
    assert "Bearer" in text and "Authorization" in text, "hook 未携带 Bearer token"
    assert "AIDUMEM_API_TOKEN" in text, "hook 未读取 token 环境变量"


def test_hook_reads_token_from_env_file_chain():
    """hook 必须能从 .env 兜底读**凭据与身份**，且不得硬编码任何绝对部署路径。

    Hermes gateway 拉起 hook 时环境几乎是空的 —— 只认环境变量就等于不认。
    同时，写死 /root/... 会让开源用户和非 root 部署直接失效。

    v19.4.2 收紧：原先只断言 token 走了兜底链，于是身份漏在链外整整一版 ——
    空环境下 hook 带着合法凭据打到 `default` 租户，HTTP 200、结果为空、日志干净，
    比 401 更难查。**凡是要发出去的键，都必须走同一条链**，所以这里逐键断言。
    """
    text = _HOOK.read_text(encoding="utf-8")
    # 不断言具体函数名：名字会随重构变（v19.4.2 就把「只取 token」的读取器
    # 推广成了通用取键器）。断言的是能力 —— 该键确实经由某个查找函数解析，
    # 而不是仅仅 `${VAR:-default}` 读一下环境变量。
    for key in ("AIDUMEM_API_TOKEN", "AIDUMEM_USER_ID"):
        assert re.search(rf"=\$\(\s*_\w+\s+{key}\b", text), (
            f"hook 未通过 .env 查找链解析 {key} —— 只认环境变量，在空环境下等于不认"
        )
    for var in ("AIDUMEM_ENV_FILE", "AIDUMEM_HOME", "HOME"):
        assert var in text, f"hook 的 .env 查找链未覆盖 {var}"
    assert not re.search(r"(?<!\w)/root/", text), "hook 硬编码了 /root 绝对路径"


def test_hook_diagnoses_auth_failure_on_stderr():
    """401/403 必须在 stderr 留痕，且**仍然 exit 0**。

    两条要求缺一不可：
      · 不留痕 → 静默失败（铁律 #8），故障可以躺一整天没人发现；
      · 不 exit 0 → 记忆服务的故障会连坐掐断用户的 LLM 调用，
        比「没有记忆」严重得多。可观测性不能以牺牲主链路为代价。
    """
    text = _HOOK.read_text(encoding="utf-8")
    assert "urllib.error.HTTPError" in text, "hook 未区分 HTTP 错误与其它异常"
    assert "auth_failed" in text, "hook 未对 401/403 输出可识别的诊断标记"
    assert 'AIDUMEM_HOOK_QUIET' in text, "hook 缺少静音开关（诊断噪声需可关闭）"
    assert '2>/dev/null"' not in text, "hook 仍在丢弃 stderr，诊断信息发不出来"


def test_hook_selftest_reports_unreachable_without_blocking(tmp_path):
    """`--selftest` 必须能独立跑通、不等 stdin、并用退出码区分故障类型。

    这是给运维的「一条命令验明正身」：部署后立刻知道 hook 到底通不通，
    而不是等某天有人想起来去翻日志。
    ⚠️ 自检块必须排在 `PAYLOAD=$(cat)` **之前** —— 否则它会阻塞在 stdin 上，
    表现为「命令卡住不返回」，比报错更难排查。
    """
    proc = subprocess.run(
        ["bash", str(_HOOK), "--selftest"],
        env={**os.environ,
             "AIDUMEM_URL": "http://127.0.0.1:9",   # discard 端口，必然连不上
             "AIDUMEM_API_TOKEN": "dummy",
             "PATH": os.environ.get("PATH", "")},
        capture_output=True, text=True, timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 4, (
        f"服务不可达应返回退出码 4，实际 {proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "unreachable" in (proc.stdout + proc.stderr).lower()


def test_hook_normal_path_never_breaks_llm_call(tmp_path):
    """正常调用路径：服务挂了、401、返回垃圾 —— hook 一律 exit 0 并原样放行 payload。

    铁律：记忆是增强，不是依赖。记忆服务的任何故障都不许影响 LLM 主链路。
    """
    payload = '{"messages":[{"role":"user","content":"hello"}]}'
    proc = subprocess.run(
        ["bash", str(_HOOK)],
        input=payload,
        env={**os.environ,
             "AIDUMEM_URL": "http://127.0.0.1:9",
             "AIDUMEM_API_TOKEN": "dummy",
             "AIDUMEM_HOOK_QUIET": "1",
             "PATH": os.environ.get("PATH", "")},
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"服务不可达时 hook 未放行：rc={proc.returncode} {proc.stderr}"
    assert proc.stdout.strip(), "hook 吞掉了 payload —— LLM 调用会失去输入"


# ═══════════════════════════════════════════════════════════════
# .env 解析：多形态兼容（症状全都长得像「没配 token」）
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("body,expected", [
    ('AIDUMEM_API_TOKEN=plain-token\n', "plain-token"),
    ('AIDUMEM_API_TOKEN="quoted-token"\n', "quoted-token"),
    ("AIDUMEM_API_TOKEN='single-quoted'\n", "single-quoted"),
    ('export AIDUMEM_API_TOKEN=exported-token\n', "exported-token"),
    ('export AIDUMEM_API_TOKEN="exported-quoted"\n', "exported-quoted"),
    ('# 注释行\n\nAIDUMEM_API_TOKEN=after-blank\n', "after-blank"),
    ('AIDUMEM_API_TOKEN=with-crlf\r\n', "with-crlf"),
])
def test_env_file_forms_all_yield_token(tmp_path, monkeypatch, body, expected):
    """.env 的多种常见写法都必须能读出 token。

    尤其是 `export KEY=VALUE`：很多部署的 .env 是给 shell `source` 用的，
    自然带 export 前缀。v19.4.2 之前 Python 侧不认这种写法而 bash 侧认 ——
    同一份文件，hook 读得到、Python 读不到，症状与「压根没配 token」一模一样，
    排查时极易怀疑到错误的方向。
    """
    from ducky.utils import api_auth_headers

    env_file = tmp_path / ".env"
    env_file.write_text(body, encoding="utf-8", newline="")
    monkeypatch.delenv("AIDUMEM_API_TOKEN", raising=False)
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(env_file))

    headers = api_auth_headers()
    assert headers.get("Authorization") == f"Bearer {expected}", (
        f"这种 .env 写法读不出 token：{body!r}"
    )


def test_env_token_precedence_and_absence(tmp_path, monkeypatch):
    """环境变量优先于 .env；两者都没有时返回空 dict（等价于门禁未启用，本机零配置可用）。"""
    from ducky.utils import api_auth_headers

    env_file = tmp_path / ".env"
    env_file.write_text("AIDUMEM_API_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(env_file))

    monkeypatch.setenv("AIDUMEM_API_TOKEN", "from-env")
    assert api_auth_headers()["Authorization"] == "Bearer from-env"

    monkeypatch.delenv("AIDUMEM_API_TOKEN", raising=False)
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(tmp_path / "absent.env"))
    assert api_auth_headers() == {}


# ═══════════════════════════════════════════════════════════════
# 部署模板：把 v19.4.1 的三个运维坑焊死在仓库里
# ═══════════════════════════════════════════════════════════════

def test_sync_daemon_dependency_is_declared():
    """inotify_simple 必须在依赖清单里声明。

    v19.4.1 它谁都没写 —— 装完跑不起来，进程起来就 ImportError 退出。
    """
    pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    reqs = (_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "inotify_simple" in pyproject, "pyproject.toml 未声明 inotify_simple"
    assert "inotify_simple" in reqs, "requirements.txt 未声明 inotify_simple"


def test_sync_unit_template_makes_crashloop_visible():
    """同步守护的 unit 模板必须让崩溃循环**看得见**。

    v19.4.1 生产的 unit 只有 Restart=always 没有 StartLimit*，于是 systemd
    无限重试、状态长期停在 `activating`（不是 failed），
    `systemctl is-active` 看着像「正在启动」—— 监控永远不报警。
    重启 6419 次、同步停摆 8 天，没有任何一处告警。
    """
    unit = (_ROOT / "deploy" / "aidumem-sync.service").read_text(encoding="utf-8")
    assert "StartLimitBurst" in unit and "StartLimitIntervalSec" in unit, (
        "unit 缺 StartLimit*，崩溃循环会永远伪装成 activating 而不进 failed"
    )
    assert "EnvironmentFile" in unit, "unit 缺 EnvironmentFile，门禁开启后同步必然 401"
    assert re.search(r"^Restart=on-failure", unit, re.M), (
        "Restart 必须是 on-failure —— always 会连正常退出也无限重启"
    )


def test_logrotate_template_uses_copytruncate():
    """日志轮转模板必须用 copytruncate。

    服务 unit 用 `StandardOutput=append:` 持有文件句柄：直接 mv 会让进程
    继续往「已改名的旧文件」里写，新文件永远是空的 —— 看着转了，其实没转。
    """
    conf = (_ROOT / "deploy" / "logrotate" / "aidumem").read_text(encoding="utf-8")
    assert "copytruncate" in conf, "logrotate 缺 copytruncate，append: 句柄会导致轮转失效"
    assert "rotate" in conf and "maxsize" in conf


def test_legacy_user_id_mapping_is_observable():
    """历史 user_id 映射的启用与否必须在日志里说清楚。

    生产上 .env 没设 AIDUMEM_LEGACY_USER_IDS，导致旧分区的记忆全部无法召回。
    最坏的地方在于：它不报错 —— 查询正常返回，只是永远查不到那些内容。
    这类「看着好好的」故障必须靠日志自曝，否则没有任何发现路径。
    """
    text = (_ROOT / "ducky" / "mem0_runtime.py").read_text(encoding="utf-8")
    assert "AIDUMEM_LEGACY_USER_IDS" in text
    assert "_legacy_map_announced" in text, "历史映射状态未做一次性播报，启用与否无从判断"
