"""Regression cases for deployment and upgrade gates that previously reported success falsely."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_hook_checker_marks_plugin_and_api_topologies_not_applicable(tmp_path):
    checker = ROOT / "scripts/check_hook_deployment.py"
    plugin_cfg = tmp_path / "plugin.yaml"
    plugin_cfg.write_text("memory:\n  provider: aidumem\n", encoding="utf-8")
    plugin = subprocess.run(
        [sys.executable, str(checker), "--config", str(plugin_cfg), "--json"],
        capture_output=True, text=True,
    )
    assert plugin.returncode == 0, plugin.stderr
    result = json.loads(plugin.stdout)
    assert result["applicability"] == "not_applicable"
    assert result["integration"] == "plugin"
    assert result["ok"] is None

    empty_cfg = tmp_path / "api.yaml"
    empty_cfg.write_text("{}\n", encoding="utf-8")
    api = subprocess.run(
        [sys.executable, str(checker), "--config", str(empty_cfg),
         "--integration", "api", "--json"], capture_output=True, text=True,
    )
    assert api.returncode == 0, api.stderr
    assert json.loads(api.stdout)["applicability"] == "not_applicable"

    strict = subprocess.run(
        [sys.executable, str(checker), "--config", str(empty_cfg),
         "--integration", "hooks", "--json"], capture_output=True, text=True,
    )
    assert strict.returncode == 1
    assert json.loads(strict.stdout)["missing_events"] == [
        "pre_llm_call", "post_llm_call", "on_session_end"]

    unknown = subprocess.run(
        [sys.executable, str(checker), "--config", str(empty_cfg), "--json"],
        capture_output=True, text=True,
    )
    assert unknown.returncode == 1
    assert json.loads(unknown.stdout)["applicability"] == "unknown"


def test_plugin_selection_does_not_excuse_a_partly_declared_shell_hook(tmp_path):
    checker = ROOT / "scripts/check_hook_deployment.py"
    host = tmp_path / "inject.sh"
    shutil.copy2(ROOT / "integrations/aidumem-inject.sh", host)
    config = tmp_path / "config.yaml"
    config.write_text(
        f'memory:\n  provider: aidumem\nhooks:\n  pre_llm_call:\n    - command: "{host}"\n',
        encoding="utf-8",
    )
    checked = subprocess.run(
        [sys.executable, str(checker), "--config", str(config), "--json"],
        capture_output=True, text=True,
    )
    assert checked.returncode == 1
    result = json.loads(checked.stdout)
    assert result["applicability"] == "hooks"
    assert result["missing_events"] == ["post_llm_call", "on_session_end"]


def test_plugin_topology_ignores_unrelated_hermes_hooks(tmp_path):
    checker = ROOT / "scripts/check_hook_deployment.py"
    other = tmp_path / "unrelated.sh"
    other.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        f'memory:\n  provider: aidumem\nhooks:\n  pre_llm_call:\n    - command: "{other}"\n',
        encoding="utf-8",
    )
    checked = subprocess.run(
        [sys.executable, str(checker), "--config", str(config), "--json"],
        capture_output=True, text=True,
    )
    assert checked.returncode == 0, checked.stderr
    result = json.loads(checked.stdout)
    assert result["applicability"] == "not_applicable"
    assert result["ignored_other_hooks"] == 1


def _cron_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in ("update_crontab.sh", "cron_manifest.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    listed = subprocess.run(
        ["bash", str(ROOT / "scripts/update_crontab.sh"), "--list"],
        check=True, text=True, capture_output=True,
    )
    for task in json.loads(listed.stdout)["tasks"]:
        target = task["command"].split("scripts/", 1)[1].split()[0]
        (scripts / target).touch()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "crontab.txt"
    fake = fake_bin / "crontab"
    fake.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "-l" ]; then\n'
        '  if [ -f "$CRON_STATE" ]; then cat "$CRON_STATE"; else exit 1; fi\n'
        'else\n'
        '  cat "$1" > "$CRON_STATE"\n'
        'fi\n', encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CRON_STATE": str(state),
        "AIDUMEM_HOME": str(repo),
        "AIDUMEM_PYTHON": sys.executable,
    }
    return scripts / "update_crontab.sh", state, env


def _cron(script: Path, env: dict[str, str], mode: str):
    return subprocess.run(["bash", str(script), mode], env=env, capture_output=True,
                          text=True, timeout=15)


def test_cron_bogus_headers_and_stale_commands_are_not_installed(tmp_path):
    script, state, env = _cron_fixture(tmp_path)
    state.write_text("\n".join(
        f"# aiduMEI:bogus_{i}|owner=none\n* * * * * echo bogus" for i in range(9)
    ) + "\n", encoding="utf-8")
    original = state.read_text(encoding="utf-8")
    data = json.loads(_cron(script, env, "--installed").stdout)
    assert data["expected"] == 9
    assert data["installed"] == 0
    assert not data["ok"]
    # The marker alone does not prove that its following cron command belongs
    # to us; install must leave the existing crontab untouched.
    assert _cron(script, env, "install").returncode != 0
    assert state.read_text(encoding="utf-8") == original

    state.write_text("", encoding="utf-8")
    assert _cron(script, env, "install").returncode == 0
    lines = state.read_text(encoding="utf-8").splitlines()
    idx = next(i for i, line in enumerate(lines) if line.startswith("# aiduMEI:report|"))
    original = lines[idx + 1]
    lines[idx + 1] = "* * * * *" + original[original.index(" cd "):]
    state.write_text("\n".join(lines) + "\n", encoding="utf-8")
    data = json.loads(_cron(script, env, "--installed").stdout)
    assert data["installed"] == 8
    assert data["tasks"]["report"]["status"] == "drift"
    assert not data["ok"]

    lines[idx + 1] = original.replace("scripts/report.py", "scripts/no_such_target.py")
    state.write_text("\n".join(lines) + "\n", encoding="utf-8")
    data = json.loads(_cron(script, env, "--installed").stdout)
    assert data["tasks"]["report"]["status"] == "drift"
    assert data["installed"] == 8


def test_cron_install_replaces_stale_entries_and_preserves_unrelated(tmp_path):
    script, state, env = _cron_fixture(tmp_path)
    repo = env["AIDUMEM_HOME"]
    unrelated = "MAILTO=ops@example.test\n0 0 * * * /usr/bin/true\n"
    # f0.3 (O-3): an entry is owned by name + root. A stale entry of *this*
    # root is replaced; one of another root (/missing) is not ours to move.
    other_root = "# aiduMEI:report|owner=old\n* * * * * cd /missing && python scripts/report.py\n"
    state.write_text(unrelated + other_root + "# aiduMEI:report|owner=old\n"
                     f'* * * * * cd "{repo}" && python scripts/report.py\n',
                     encoding="utf-8")
    first = _cron(script, env, "install")
    assert first.returncode == 0, first.stderr
    one = state.read_text(encoding="utf-8")
    assert one.startswith(unrelated + other_root)
    assert f'* * * * * cd "{repo}" && python scripts/report.py' not in one
    data = json.loads(_cron(script, env, "--installed").stdout)
    assert data["ok"]
    assert data["foreign"] == [{"line": 3, "task": "report", "root": "/missing",
                                "root_present": False}]
    second = _cron(script, env, "install")
    assert second.returncode == 0, second.stderr
    assert state.read_text(encoding="utf-8") == one


def test_cron_install_refuses_dangling_or_unknown_header_before_independent_job(tmp_path):
    script, state, env = _cron_fixture(tmp_path)
    independent = "MAILTO=ops@example.test\n0 0 * * * /usr/bin/true\n"
    for marker in ("# aiduMEI:report|owner=old",
                   "# aiduMEI:unknown_custom|owner=old"):
        original = f"{marker}\n{independent}"
        state.write_text(original, encoding="utf-8")
        installed = _cron(script, env, "install")
        assert installed.returncode != 0
        assert "ambiguous aiduMEI cron entry" in installed.stderr
        assert state.read_text(encoding="utf-8") == original

    # Mentioning an aiduMEI script as an argument does not mean that the
    # independent command actually runs that script.
    original = ("# aiduMEI:report|owner=old\n"
                "0 0 * * * cd /tmp && /bin/echo scripts/report.py >> /tmp/report.log 2>&1\n")
    state.write_text(original, encoding="utf-8")
    installed = _cron(script, env, "install")
    assert installed.returncode != 0
    assert "ambiguous aiduMEI cron entry" in installed.stderr
    assert state.read_text(encoding="utf-8") == original


def test_cron_exact_text_with_missing_target_is_not_installed(tmp_path):
    script, _state, env = _cron_fixture(tmp_path)
    assert _cron(script, env, "install").returncode == 0
    (script.parent / "report.py").unlink()
    checked = _cron(script, env, "--installed")
    assert checked.returncode == 0, checked.stderr
    data = json.loads(checked.stdout)
    assert data["installed"] == 8
    assert data["tasks"]["report"]["status"] == "target_missing"
    assert data["tasks"]["report"]["target_ok"] is False
    assert not data["ok"]


def test_cron_exact_text_with_nonexecutable_interpreter_is_not_installed(tmp_path):
    script, _state, env = _cron_fixture(tmp_path)
    fake_python = tmp_path / "fake-python"
    fake_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)
    env["AIDUMEM_PYTHON"] = str(fake_python)
    assert _cron(script, env, "install").returncode == 0
    fake_python.chmod(0o644)
    checked = _cron(script, env, "--installed")
    assert checked.returncode == 0, checked.stderr
    data = json.loads(checked.stdout)
    assert data["tasks"]["report"]["status"] == "target_missing"
    assert data["tasks"]["report"]["target_ok"] is False
    assert not data["ok"]


def test_cron_duplicate_task_and_extra_header_keep_report_yellow(tmp_path):
    import importlib.util

    script, state, env = _cron_fixture(tmp_path)
    assert _cron(script, env, "install").returncode == 0
    current = state.read_text(encoding="utf-8")
    header = next(line for line in current.splitlines() if line.startswith("# aiduMEI:report|"))
    lines = current.splitlines()
    index = lines.index(header)
    state.write_text(current + "\n" + header + "\n" + lines[index + 1] + "\n",
                     encoding="utf-8")
    data = json.loads(_cron(script, env, "--installed").stdout)
    assert data["tasks"]["report"]["status"] == "duplicate"
    assert data["installed"] == 8

    spec = importlib.util.spec_from_file_location("_ops_report", ROOT / "scripts/report.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    report = {"health_status": "ok", "degraded": [], "warming_up": [], "anomalies": {},
              "maintenance": {"crontab_task_count": 9, "crontab_installed_count": 9,
                              "crontab_verified": False,
                              "latest_backup": {"verified": True}}}
    assert module._exit_code(report) == 2
    assert any("update_crontab" in action for action in
               module._safe_next_actions(report, report["maintenance"]))


def test_canonical_prompt_cron_count_matches_manifest():
    prompt = (ROOT / "prompts/install.txt").read_text(encoding="utf-8")
    assert prompt == (ROOT / "ONE_LINE_INSTALL.md").read_text(encoding="utf-8")
    tasks = json.loads(subprocess.run(
        ["bash", str(ROOT / "scripts/update_crontab.sh"), "--list"],
        check=True, capture_output=True, text=True,
    ).stdout)["tasks"]
    assert f"安装 {len(tasks)} 项定时任务" in prompt
    assert f"实文 {len(tasks)} 条" in prompt
    assert "匹配才算装上" in prompt


def test_upgrade_smoke_gate_fails_missing_script_and_runs_real_one(tmp_path):
    helper = ROOT / "scripts/upgrade_gate_common.sh"
    assert helper.is_file()
    cmd = f'REPO_ROOT="{tmp_path}"; VENV_PY="{sys.executable}"; source "{helper}"; run_upgrade_smoke'
    absent = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    assert absent.returncode != 0
    smoke = tmp_path / "scripts/e2e_smoke.py"
    smoke.parent.mkdir()
    smoke.write_text('import sys\nassert sys.argv[1:] == ["--json"]\n'
                     'print("{\\"status\\": \\"PASS\\", \\"failures\\": 0, \\"warnings\\": 0}")\n',
                     encoding="utf-8")
    present = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    assert present.returncode == 0, present.stderr
    smoke.write_text('raise SystemExit(1)\n', encoding="utf-8")
    failed = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    assert failed.returncode != 0
    smoke.write_text('print("{\\"status\\": \\"WARN\\", \\"failures\\": 0, \\"warnings\\": 1}")\n',
                     encoding="utf-8")
    misleading = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    assert misleading.returncode != 0


def test_upgrade_scripts_use_portable_timer_and_live_smoke():
    helper = ROOT / "scripts/upgrade_gate_common.sh"
    shell = helper.read_text(encoding="utf-8")
    assert "time.monotonic_ns" in shell
    for script in ("pre-upgrade-check.sh", "post-upgrade-check.sh"):
        src = (ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert "date +%s%3N" not in src
        assert "run_upgrade_smoke" in src
        assert "upgrade_now_ms" in src
    cmd = f'VENV_PY="{sys.executable}"; source "{helper}"; a=$(upgrade_now_ms); b=$(upgrade_now_ms); test "$b" -ge "$a"'
    assert subprocess.run(["bash", "-c", cmd], capture_output=True).returncode == 0


def test_upgrade_gate_prefers_target_venv_and_runs_smoke_with_it(tmp_path):
    helper = ROOT / "scripts/upgrade_gate_common.sh"
    repo = tmp_path / "target"
    (repo / "venv/bin").mkdir(parents=True)
    (repo / ".venv/bin").mkdir(parents=True)
    (repo / "scripts").mkdir()
    production_python = repo / "venv/bin/python3"
    marker = tmp_path / "selected-python.txt"
    production_python.write_text(
        f'#!/bin/sh\necho production >> "$PY_MARKER"\nexec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    production_python.chmod(0o755)
    (repo / ".venv/bin/python").symlink_to(sys.executable)
    (repo / "scripts/e2e_smoke.py").write_text(
        'print("{\\"status\\": \\"PASS\\", \\"failures\\": 0, \\"warnings\\": 0}")\n',
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k != "AIDUMEM_PYTHON"}
    env["PY_MARKER"] = str(marker)
    command = 'REPO_ROOT="$1"; source "$2"; VENV_PY="$(upgrade_python_for_repo "$REPO_ROOT")"; ' \
              'test "$VENV_PY" = "$REPO_ROOT/venv/bin/python3" && run_upgrade_smoke'
    result = subprocess.run(["bash", "-c", command, "_", str(repo), str(helper)],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8").count("production") >= 2

    production_python.unlink()
    fallback = subprocess.run(
        ["bash", "-c", 'source "$2"; upgrade_python_for_repo "$1"',
         "_", str(repo), str(helper)], env=env, capture_output=True, text=True,
    )
    assert fallback.returncode == 0
    assert fallback.stdout.strip() == str(repo / ".venv/bin/python")
