"""f0.3 remediation: scripts/acceptance_check.sh must measure the world.

B6 fixed four gate defects; each block is extracted from the real script by
its `# >>> name` / `# <<< name` markers and run under /bin/bash (3.2 on macOS)
and the bash on PATH, against temp trees and a fake `crontab`:

  interpreter  production uses ${ROOT}/venv (no dot); only .venv was probed
  cron         the count check compared TASKS with its own --list (a
               tautology); it now reads update_crontab.sh --installed, or
               prints an explicit SKIP when there is nothing to compare
  py_compile   `py_compile $(git ls-files | head -400) 2>/dev/null` skipped
               every file after the 400th and hid the errors
  DOC-         checks that only read documentation say so in their label
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ACCEPT = ROOT / "scripts/acceptance_check.sh"

_SHELLS = [pytest.param("/bin/bash", id="bin-bash")]
if shutil.which("bash"):
    _SHELLS.append(pytest.param(shutil.which("bash"), id="path-bash"))


def _block(name: str) -> str:
    src = ACCEPT.read_text(encoding="utf-8")
    m = re.search(rf"^# >>> {name}\n(.*?)^# <<< {name}\n", src, re.S | re.M)
    assert m, f"marker block {name} missing from acceptance_check.sh"
    return m.group(1)


def _check_function() -> str:
    src = ACCEPT.read_text(encoding="utf-8")
    m = re.search(r"^check\(\) \{\n.*?^\}\n", src, re.S | re.M)
    assert m, "check() missing from acceptance_check.sh"
    return m.group(0)


def _run_block(shell: str, body: str, *, cwd: Path, env: dict[str, str], args=()):
    script = ("set -euo pipefail\nerrors=0\n" + _check_function() + body
              + '\nprintf "errors=%s\\n" "$errors"\n')
    return subprocess.run([shell, "-c", script, "acceptance-block", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)


def _wrapper(path: Path, *, with_pytest: bool) -> Path:
    """An interpreter stand-in: the real one, or one that cannot import pytest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if with_pytest:
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    else:
        path.write_text('#!/bin/sh\ncase "$*" in *pytest*) exit 1 ;; esac\n'
                        f'exec "{sys.executable}" "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# interpreter detection
# ---------------------------------------------------------------------------

def _detect(shell: str, tree: Path, path_dir: Path) -> str:
    body = 'ROOT="$1"\n' + _block("gate-interpreter") + 'printf "PY=%s\\n" "${PY}"\n'
    # PATH holds only the stand-ins: no system python can decide the verdict.
    env = {"PATH": str(path_dir), "HOME": str(tree)}
    result = _run_block(shell, body, cwd=tree, env=env, args=(str(tree),))
    assert result.returncode == 0, result.stderr
    return re.search(r"^PY=(.*)$", result.stdout, re.M).group(1)


@pytest.mark.parametrize("shell", _SHELLS)
def test_production_venv_without_a_dot_is_found(shell, tmp_path):
    tree, empty = tmp_path / "tree", tmp_path / "empty-path"
    empty.mkdir()
    venv = _wrapper(tree / "venv/bin/python", with_pytest=True)
    assert _detect(shell, tree, empty) == str(venv)


@pytest.mark.parametrize("shell", _SHELLS)
def test_detection_order_and_pytest_requirement(shell, tmp_path):
    tree, path_dir = tmp_path / "tree", tmp_path / "path"
    dot = _wrapper(tree / ".venv/bin/python", with_pytest=True)
    _wrapper(tree / "venv/bin/python", with_pytest=True)
    python3 = _wrapper(path_dir / "python3", with_pytest=True)
    assert _detect(shell, tree, path_dir) == str(dot), ".venv keeps its priority"
    # Control: an in-tree venv that cannot import pytest does not win.
    _wrapper(tree / ".venv/bin/python", with_pytest=False)
    _wrapper(tree / "venv/bin/python", with_pytest=False)
    assert _detect(shell, tree, path_dir) == "python3"
    python3.unlink()
    assert _detect(shell, tree, path_dir) == ""


# ---------------------------------------------------------------------------
# py_compile: every tracked file, errors shown, count asserted
# ---------------------------------------------------------------------------

def _git_tree(tmp_path: Path, good: int, bad_names=()) -> Path:
    tree = tmp_path / "gittree"
    (tree / "pkg").mkdir(parents=True)
    for index in range(good):
        (tree / "pkg" / f"m{index:03d}.py").write_text(f"VALUE = {index}\n", encoding="utf-8")
    for name in bad_names:
        (tree / "pkg" / name).write_text("def broken(:\n", encoding="utf-8")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull}
    subprocess.run(["git", "init", "-q"], cwd=tree, env=env, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tree, env=env, check=True)
    return tree


def _py_compile(shell: str, tree: Path):
    body = 'PY="$1"\n' + _block("gate-py-compile")
    env = {"PATH": os.environ["PATH"], "HOME": str(tree), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull}
    return _run_block(shell, body, cwd=tree, env=env, args=(sys.executable,))


@pytest.mark.parametrize("shell", _SHELLS)
def test_a_broken_file_after_the_400th_fails_the_gate(shell, tmp_path):
    # "zzz" sorts last: f0.2's `head -400` never compiled it.
    tree = _git_tree(tmp_path, 400, bad_names=("zzz_broken.py",))
    listed = subprocess.run(["git", "ls-files", "*.py"], cwd=tree, capture_output=True,
                            text=True, check=True).stdout.splitlines()
    assert listed.index("pkg/zzz_broken.py") >= 400
    result = _py_compile(shell, tree)
    assert "FAIL hard gate: py_compile passes" in result.stderr
    assert "does not compile: pkg/zzz_broken.py" in result.stderr
    assert "400/401 git-tracked .py files compile" in result.stderr
    assert "errors=1" in result.stdout


@pytest.mark.parametrize("shell", _SHELLS)
def test_all_tracked_files_compile_and_the_count_is_shown(shell, tmp_path):
    """Control: the same harness passes a healthy tree and writes no .pyc."""
    tree = _git_tree(tmp_path, 12)
    result = _py_compile(shell, tree)
    assert "PASS hard gate: py_compile passes (12/12 git-tracked .py files compile)" \
        in result.stdout
    assert "errors=0" in result.stdout
    assert not list(tree.rglob("*.pyc"))


@pytest.mark.parametrize("shell", _SHELLS)
def test_nothing_to_compile_or_no_git_is_not_a_pass(shell, tmp_path):
    empty = _git_tree(tmp_path, 0)
    assert "errors=1" in _py_compile(shell, empty).stdout      # 0 files is not 0 failures
    plain = tmp_path / "not-a-checkout"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
    result = _py_compile(shell, plain)
    assert "errors=1" in result.stdout
    assert "cannot enumerate tracked .py files" in result.stderr
    deleted = _git_tree(tmp_path / "deleted", 3)
    (deleted / "pkg/m000.py").unlink()                         # tracked but gone
    assert "errors=1" in _py_compile(shell, deleted).stdout


# ---------------------------------------------------------------------------
# cron: the installed crontab, or an explicit SKIP
# ---------------------------------------------------------------------------

def _checkout(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for script in ("update_crontab.sh", "cron_manifest.py"):
        shutil.copy2(ROOT / "scripts" / script, scripts / script)
    listed = subprocess.run(["bash", str(ROOT / "scripts/update_crontab.sh"), "--list"],
                            check=True, text=True, capture_output=True)
    for task in json.loads(listed.stdout)["tasks"]:
        (scripts / task["command"].split("scripts/", 1)[1].split()[0]).touch()
    return repo


def _cron_env(tmp_path: Path, repo: Path) -> dict[str, str]:
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    crontab = fake / "crontab"
    crontab.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "-l" ]; then\n'
        '  if [ -f "$CRON_STATE" ]; then cat "$CRON_STATE"; else exit 1; fi\n'
        'else\n'
        '  cat "$1" > "$CRON_STATE"\n'
        'fi\n', encoding="utf-8")
    crontab.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "AIDUMEI_CRON_RUN_AS"}
    env.update({"PATH": f"{fake}:{os.environ['PATH']}", "CRON_STATE": str(tmp_path / "crontab.txt"),
                "AIDUMEM_HOME": str(repo), "AIDUMEM_PYTHON": sys.executable})
    return env


def _cron_gate(shell: str, repo: Path, env: dict[str, str]):
    return _run_block(shell, 'ROOT="$1"\n' + _block("gate-cron-installed"), cwd=repo, env=env,
                      args=(str(repo),))


def _install(repo: Path, env: dict[str, str]):
    done = subprocess.run(["bash", str(repo / "scripts/update_crontab.sh"), "install"],
                          env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("shell", _SHELLS)
def test_cron_gate_skips_explicitly_without_a_crontab(shell, tmp_path):
    repo = _checkout(tmp_path)
    result = _cron_gate(shell, repo, _cron_env(tmp_path, repo))
    assert "SKIP cron installed-state: no crontab" in result.stdout
    assert "PASS" not in result.stdout and "errors=0" in result.stdout


@pytest.mark.parametrize("shell", _SHELLS)
def test_cron_gate_skips_a_crontab_without_entries_for_this_checkout(shell, tmp_path):
    repo, other = _checkout(tmp_path), _checkout(tmp_path, "other-checkout")
    state = tmp_path / "crontab.txt"
    state.write_text("MAILTO=ops@example.test\n0 0 * * * /usr/bin/true\n", encoding="utf-8")
    env = _cron_env(tmp_path, repo)
    assert "holds no aiduMEI entries" in _cron_gate(shell, repo, env).stdout
    _install(other, _cron_env(tmp_path, other))      # only another root's entries
    result = _cron_gate(shell, repo, env)
    assert "holds no aiduMEI entries" in result.stdout and "errors=0" in result.stdout


@pytest.mark.parametrize("shell", _SHELLS)
def test_cron_gate_passes_only_when_the_installed_crontab_matches(shell, tmp_path):
    repo = _checkout(tmp_path)
    env = _cron_env(tmp_path, repo)
    state = tmp_path / "crontab.txt"
    _install(repo, env)
    expected = len(json.loads(subprocess.run(
        ["bash", str(repo / "scripts/update_crontab.sh"), "--list"], env=env,
        capture_output=True, text=True, check=True).stdout)["tasks"])
    good = _cron_gate(shell, repo, env)
    assert f"PASS cron installed-state matches the manifest (update_crontab.sh --installed: " \
           f"{expected}/{expected})" in good.stdout
    # Discriminating controls: one task missing, then a legacy duplicate line.
    lines = state.read_text(encoding="utf-8").splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("# aiduMEI:report|"))
    state.write_text("\n".join(lines[:header] + lines[header + 2:]) + "\n", encoding="utf-8")
    missing = _cron_gate(shell, repo, env)
    assert f"FAIL cron installed-state matches the manifest (update_crontab.sh --installed: " \
           f"{expected - 1}/{expected})" in missing.stderr
    assert "errors=1" in missing.stdout
    _install(repo, env)
    legacy = (f"0 4 * * * runuser -u aidumem -- {repo}/venv/bin/python3 "
              f"{repo}/scripts/consolidator.py\n")
    state.write_text(legacy + state.read_text(encoding="utf-8"), encoding="utf-8")
    duplicated = _cron_gate(shell, repo, env)
    assert "FAIL cron installed-state" in duplicated.stderr and "errors=1" in duplicated.stdout


# ---------------------------------------------------------------------------
# DOC- labels
# ---------------------------------------------------------------------------

_DOC_SUFFIXES = (".md", ".txt", ".example")
_PATH_RE = re.compile(r"[\w./-]+\.(?:md|txt|example|py|sh|service|json|toml)\b")


def _checks() -> list[tuple[str, str]]:
    src = ACCEPT.read_text(encoding="utf-8")
    pattern = re.compile(r'^check "([^"]+)"(.*?)(?=^check "|^#|^[A-Z_]+=|^if |^fi\b|^printf |\Z)',
                         re.S | re.M)
    return [(m.group(1), m.group(2)) for m in pattern.finditer(src)]


def test_documentation_only_checks_carry_the_doc_prefix():
    checks = _checks()
    assert len(checks) >= 20, "the label parser lost its range"
    doc_only = []
    for label, command in checks:
        paths = _PATH_RE.findall(command)
        runs_code = re.search(r"\b(?:bash|python3?)\s+scripts/|\"\$1\"|\benv\b", command)
        if paths and not runs_code and all(p.endswith(_DOC_SUFFIXES) for p in paths):
            doc_only.append(label)
    assert doc_only, "the classifier found no documentation check at all"
    unlabeled = [label for label in doc_only if not label.startswith("DOC-")]
    assert not unlabeled, f"documentation-only checks without the DOC- prefix: {unlabeled}"
    # Control: behaviour checks are not relabelled as documentation.
    for label in ("no hardcoded green probes", "dependency declarations match",
                  "hard gate: push_gate exits 0"):
        assert any(existing == label for existing, _ in checks), label
