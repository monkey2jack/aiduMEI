"""v20.3.2 正式版 · 汇报与验收的接缝：P0-1 元数据锚定 / P0-2 health 公开视图 / P0-3 假硬关 / P1-16 drill / P0-6 台账。

用户审计三条 🔴 的共同形态：**代码逐行都对，错在「说的」与「跑的」接缝处。**
  · A：report.py 自报的 commit 不在任何 tag 血脉上，却退出 0 —— 替一台脱锚的机器背书；
  · B：一行 Prompt 第 7 步要读的字段被自家脱敏删掉 —— 越按文档做越走不通；
  · C：「hard gate: push_gate exits 0」判据只测执行位 —— 用例名说谎，与 P2-16 同型同文件。
再加我自己交代的「乙」：drill 5 项判据 4 项恒真。
"""
import ast
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ══════════════════════════════════════════════════════════════
# P0-1 · report.py 必须能说出「我是哪个发布点，脏了多少」
# ══════════════════════════════════════════════════════════════

def test_report_exposes_git_describe_with_anchoring_verdict():
    rep = _load_script("report")
    gd = rep._git_describe()
    for k in ("describe", "exact_tag", "dirty", "anchored"):
        assert k in gd, f"git_describe 缺字段 {k}"
    assert isinstance(gd["anchored"], bool)


def test_unanchored_machine_cannot_get_exit_code_zero():
    """**P0-1 靶心**：不在 tag 上 / 工作树脏 → 退出码 2，不许 0。"""
    rep = _load_script("report")
    happy = {
        "health_status": "ok", "degraded": [], "warming_up": [],
        "anomalies": {"warnings": []},
        "maintenance": {"crontab_task_count": 8, "crontab_installed_count": 8,
                        "latest_backup": {"verified": True}},
    }
    dirty = dict(happy, git_describe={"describe": "v20.3-13-g99e0a37-dirty", "exact_tag": None,
                                       "dirty": True, "anchored": False})
    assert rep._exit_code(dirty) == 2, "脱锚的机器拿到了退出码 0 —— report.py 替它背书了"
    off_tag = dict(happy, git_describe={"describe": "v20.3.2-3-gabc1234", "exact_tag": None,
                                         "dirty": False, "anchored": False})
    assert rep._exit_code(off_tag) == 2
    anchored = dict(happy, git_describe={"describe": "v20.3.2", "exact_tag": "v20.3.2",
                                          "dirty": False, "anchored": True})
    assert rep._exit_code(anchored) == 0, "锚定且健康的机器应为 0"


def test_public_report_carries_git_describe():
    rep = _load_script("report")
    out = rep._public_report({"health_status": "ok", "status": "ok", "degraded": [], "warming_up": []})
    assert "git_describe" in out and "git_commit" in out


# ══════════════════════════════════════════════════════════════
# P0-2 · /health 公开视图：留键说明，不删键；门禁未启用放行全部
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def health_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "l"))
    import api_server as A  # noqa: F401
    import ducky.hot.health as H
    monkeypatch.setitem(H._PUBLIC_CACHE, "full", None)
    monkeypatch.setitem(H._PUBLIC_CACHE, "ts", 0.0)
    return A, H


def _client(A):
    from fastapi.testclient import TestClient
    return TestClient(A.app, raise_server_exceptions=False)


def test_gate_on_anonymous_keeps_keys_and_explains(health_env, monkeypatch):
    """**P0-2 靶心**：门禁启用 + 未授权 → probes 键**在**，带 _redacted 说明，且 data_dir_writable 可读。"""
    A, H = health_env
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "probe-token-not-a-real-secret")
    j = _client(A).get("/health").json()
    assert "probes" in j, "probes 键被删了 —— jq '.probes.runtime_paths' 得 null，agent 只能判失败"
    assert isinstance(j["probes"].get("_redacted"), str), "没告诉调用方「是你没带凭据」"
    rp = j["probes"].get("runtime_paths") or {}
    assert isinstance(rp.get("data_dir_writable"), bool), "第 7 步要读的 data_dir_writable 拿不到"
    assert "data_dir" not in rp, "未授权不该泄绝对路径"


def test_gate_on_authorized_gets_full_probes(health_env, monkeypatch):
    A, H = health_env
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "probe-token-not-a-real-secret")
    j = _client(A).get("/health", headers={"Authorization": "Bearer probe-token-not-a-real-secret"}).json()
    assert "_redacted" not in j.get("probes", {}), "带凭据仍被脱敏"
    assert isinstance((j["probes"].get("runtime_paths") or {}).get("data_dir"), str)
    assert "mem0_patches" in j["probes"], "P0-6：补丁台账没上 /health"


def test_gate_off_returns_full_probes_to_everyone(health_env, monkeypatch, tmp_path):
    """门禁未启用时不存在需要防的侦察者 —— 把字段藏给唯一有权看的人，安全收益为零。"""
    A, H = health_env
    for k in ("AIDUMEM_API_TOKEN", "AIDUMEM_UI_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    import ducky.security.auth as _auth
    monkeypatch.setattr(_auth, "password_hash_path", lambda: str(tmp_path / "h"))
    A._ensure_ui_password()
    assert A._auth_enabled() is False
    j = _client(A).get("/health").json()
    assert "probes" in j and "_redacted" not in j["probes"]
    assert isinstance((j["probes"].get("runtime_paths") or {}).get("data_dir"), str)


def test_anonymous_health_is_cached_not_recomputed(health_env, monkeypatch):
    """Codex F-12：匿名高频探测不许每次重跑 640 行探针。30s 内命中缓存 → ts 不变。"""
    A, H = health_env
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "probe-token-not-a-real-secret")
    c = _client(A)
    c.get("/health")
    ts1 = H._PUBLIC_CACHE["ts"]
    assert ts1 > 0
    c.get("/health")
    assert H._PUBLIC_CACHE["ts"] == ts1, "第二次匿名请求重跑了全量探针（缓存没生效）"


def _resolve(obj, dotted: str):
    cur = obj
    for part in dotted.strip(".").split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def test_documented_health_recipes_are_executable(health_env, monkeypatch):
    """**文档里的可执行判据必须可执行**（P0-3 那一课在 install.txt 上又犯了一次）。

    解析 prompts/install.txt 与 AGENTS.md 里出现的 /health 字段路径，在「装完 + 按第 6 步
    配了 token」的形态下**带凭据**真打一次，每个路径都必须非 null；
    再**不带凭据**打一次，第 7 步依赖的 data_dir_writable 仍必须可读。
    """
    A, H = health_env
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "probe-token-not-a-real-secret")
    text = (_ROOT / "prompts" / "install.txt").read_text(encoding="utf-8") + \
           (_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    paths = set(re.findall(r"(?<![\w/])\.?((?:probes\.)?runtime_paths(?:\.[a-z_]+)?|health_status|degraded)\b", text))
    paths = {p if p.startswith("probes.") or "." not in p else "probes." + p for p in paths}
    assert paths, "文档里没找到任何 /health 字段路径 —— 守卫失去着力点"
    c = _client(A)
    with_token = c.get("/health", headers={"Authorization": "Bearer probe-token-not-a-real-secret"}).json()
    for p in sorted(paths):
        assert _resolve(with_token, p) is not None, f"文档配方字段 {p} 带凭据仍为 null"
    anon = c.get("/health").json()
    assert _resolve(anon, "probes.runtime_paths.data_dir_writable") is not None, (
        "不带凭据时第 7 步的 data_dir_writable 为 null —— 新用户会在这一步停工")
    assert "Authorization" in (_ROOT / "prompts" / "install.txt").read_text(encoding="utf-8"), (
        "install.txt 第 7 步没告诉 agent 带凭据")


# ══════════════════════════════════════════════════════════════
# P0-3 · 验收脚本不许有假硬关；push_gate 必须可移植
# ══════════════════════════════════════════════════════════════

def test_no_exits_zero_check_is_just_an_executable_bit_test():
    """**元守卫**：用例名说「exits 0」，判据就得真调用；只 `test -x/-f` = 用例名说谎。"""
    src = (_ROOT / "scripts" / "acceptance_check.sh").read_text(encoding="utf-8")
    liars = []
    for line in src.splitlines():
        m = re.match(r'\s*check\s+"([^"]*exits 0[^"]*)"\s+(.*)$', line)
        if not m:
            continue
        judge = m.group(2)
        if re.fullmatch(r"bash -c 'test -[xf] [^']*'", judge.strip()):
            liars.append(m.group(1))
    assert not liars, f"这些「exits 0」检查只测了文件存在/执行位，从没跑过：{liars}"


def test_push_gate_is_portable_bash_with_interpreter_detection():
    src = (_ROOT / "scripts" / "push_gate.sh").read_text(encoding="utf-8")
    assert src.startswith("#!/usr/bin/env bash"), "push_gate 仍是 zsh —— 生产只有 bash"
    assert "${(@f)" not in src, "残留 zsh 专有数组语法"
    assert not re.search(r"^\s*(mapfile|readarray)\b", src, re.M), (
        "mapfile/readarray 是 bash 4+ 内建；macOS 自带 /bin/bash 3.2 上直接 command not found（退出码 127）")
    assert "AIDUMEM_PYTHON" in src and "./venv/bin/python" in src, "解释器仍写死 .venv/"
    assert 'import ruff' in src, "ruff 缺席时没有显式处置（会静默假绿或假红）"
    r = subprocess.run(["bash", "-n", str(_ROOT / "scripts" / "push_gate.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # 解释器路径含空格（本机 `4. Web/`）时 `$PY -m pytest` 被切词 → 测试关直接报「No such file」。
    # 生产路径无空格所以从没暴露；push_gate 里每一处 $PY 调用都必须带双引号。
    unquoted = re.findall(r'(?<!")\$PY(?![\w}"])', src)
    assert not unquoted, f"push_gate 里有 {len(unquoted)} 处未加引号的 $PY 调用（路径含空格即切词）"


def test_acceptance_hard_gate_really_invokes_push_gate():
    src = (_ROOT / "scripts" / "acceptance_check.sh").read_text(encoding="utf-8")
    line = next(l for l in src.splitlines() if 'hard gate: push_gate exits 0' in l)
    assert "bash scripts/push_gate.sh" in line, f"硬关仍未真跑 push_gate：{line.strip()}"


# ══════════════════════════════════════════════════════════════
# P1-16 · drill 的判据必须有区分力
# ══════════════════════════════════════════════════════════════

def _drill_python_block() -> str:
    src = (_ROOT / "scripts" / "drill_autoshift.sh").read_text(encoding="utf-8")
    m = re.search(r"python3 -c '\n(.*?)\n' \"\$\{HEALTH_FILE\}\"", src, re.S)
    assert m, "drill 脚本里找不到内嵌 python 判据块 —— 守卫失去着力点"
    return m.group(1)


def _run_drill_judge(payload: dict, tmp_path) -> tuple[int, dict]:
    code = _drill_python_block()
    h = tmp_path / "h.json"; o = tmp_path / "o.json"
    h.write_text(json.dumps(payload), encoding="utf-8")
    r = subprocess.run([sys.executable, "-c", code, str(h), str(o)], capture_output=True, text=True)
    return r.returncode, (json.loads(o.read_text()) if o.exists() else {})


def test_drill_fails_on_redacted_health(tmp_path):
    """**P1-16 靶心**：拿到脱敏 /health（探针全空）必须 fail，并说明原因。修复前 4/5 恒真。"""
    redacted = {"status": "ok", "health_status": "ok", "degraded": [], "warming_up": [],
                "probes": {"_redacted": "authenticate", "runtime_paths": {"data_dir_writable": True}}}
    rc, out = _run_drill_judge(redacted, tmp_path)
    assert out.get("status") == "fail", f"探针全空仍报 pass：{out.get('checks')}"
    assert rc != 0
    assert "_why" in out.get("checks", {}), "fail 了却不说是因为探针被脱敏"


def test_drill_passes_on_real_full_health(tmp_path):
    full = {"status": "ok", "health_status": "ok", "degraded": [], "warming_up": [],
            "probes": {"engine_mode_policy": {"configured": "auto"},
                       "llm_gear": {"gear": "cloud", "state": "closed"},
                       # 形状取自 2026-09-03 生产 /health 实读（不是我造的）：第一版用例写成
                       # {"pending_count": 0}，与 drill 判据互相印证 —— 两边都错在同一个地方，守卫等于零。
                       "pending_embeddings": {"cloud": 0, "local": 0, "last_replay": None,
                                              "verdict": {"level": "ok", "total": 0, "warn_at": 500}}}}
    rc, out = _run_drill_judge(full, tmp_path)
    assert out.get("status") == "pass", out.get("checks")
    assert rc == 0
    legacy = dict(full, probes=dict(full["probes"], pending_embeddings={"pending_count": 0}))
    rc2, out2 = _run_drill_judge(legacy, tmp_path)
    assert out2.get("status") == "pass", "旧形状（pending_count）也应被接受"


def test_drill_fails_when_probes_are_empty_dicts(tmp_path):
    """原判据 `isinstance({}, dict)` 恒真 —— 空字典必须判 fail。"""
    hollow = {"status": "ok", "health_status": "ok", "degraded": [], "warming_up": [],
              "probes": {"engine_mode_policy": {"configured": "auto"}, "llm_gear": {}, "pending_embeddings": {}}}
    rc, out = _run_drill_judge(hollow, tmp_path)
    assert out.get("status") == "fail", "空探针字典仍 pass —— 判据没区分力"
