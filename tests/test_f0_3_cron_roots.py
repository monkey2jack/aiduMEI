"""f0.3 remediation: a cron entry belongs to one checkout (task name + root).

O-3  f0.2's merge matched existing entries by task name and relative
     scripts/<file> only, so `update_crontab.sh install` from a sandbox next
     to production silently took over production's managed entries.
dup  --installed now also reports job lines outside the managed block that
     run this root's managed scripts (production still carries a legacy
     unmanaged `runuser -u aidumem -- .../scripts/consolidator.py` line: the
     consolidator ran twice a day) as drift.
run  AIDUMEI_CRON_RUN_AS lets a root installer demote the data-writing
     consolidator with `runuser -u <user> --` (validated name).

Every test drives the real update_crontab.sh under /bin/bash (3.2 on macOS)
and the bash on PATH, with a fake `crontab` first on PATH and temp checkouts.
The real crontab is never read or written.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

_SHELLS = [pytest.param("/bin/bash", id="bin-bash")]
if shutil.which("bash"):
    _SHELLS.append(pytest.param(shutil.which("bash"), id="path-bash"))


def _checkout(tmp_path: Path, name: str) -> Path:
    """A minimal checkout: the installer, its verifier and every task target."""
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


def _fake_bin(tmp_path: Path, *, root: bool = False) -> Path:
    fake = tmp_path / "bin"
    fake.mkdir(parents=True, exist_ok=True)
    crontab = fake / "crontab"
    crontab.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "-l" ]; then\n'
        '  if [ -f "$CRON_STATE" ]; then cat "$CRON_STATE"; else exit 1; fi\n'
        'else\n'
        '  cat "$1" > "$CRON_STATE"\n'
        'fi\n', encoding="utf-8")
    crontab.chmod(0o755)
    # The installer's identity is always faked: whoever runs the suite (root
    # in a container, a developer elsewhere) must not change the verdict.
    uid, login = ("0", "root") if root else ("1000", "tester")
    (fake / "id").write_text(
        '#!/bin/sh\n'
        'case "$*" in\n'
        f'  "-u") echo {uid} ;;\n'
        f'  "-un") echo {login} ;;\n'
        '  "-u aidumem") echo 990 ;;\n'
        '  *) exit 1 ;;\n'
        'esac\n', encoding="utf-8")
    (fake / "id").chmod(0o755)
    if root:
        # A root installer on a host that has the demotion user and runuser.
        (fake / "runuser").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake / "chown").write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CHOWN_LOG"\n', encoding="utf-8")
        for name in ("runuser", "chown"):
            (fake / name).chmod(0o755)
    return fake


def _env(tmp_path: Path, repo: Path, fake: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "AIDUMEI_CRON_RUN_AS"}
    env.update({
        "PATH": f"{fake}:{os.environ['PATH']}",
        "CRON_STATE": str(tmp_path / "crontab.txt"),
        "CHOWN_LOG": str(tmp_path / "chown.log"),
        "AIDUMEM_HOME": str(repo),
        "AIDUMEM_PYTHON": sys.executable,
        "AIDUMEM_DATA_DIR": str(tmp_path / "data"),
        "AIDUMEM_LOG_DIR": str(tmp_path / "logs"),
    })
    env.update(extra)
    return env


def _cron(shell: str, repo: Path, env: dict[str, str], mode: str):
    return subprocess.run([shell, str(repo / "scripts/update_crontab.sh"), mode],
                          env=env, capture_output=True, text=True, timeout=30)


def _lines_of(state: Path, root: Path) -> list[str]:
    lines = state.read_text(encoding="utf-8").splitlines()
    return [line for index, line in enumerate(lines)
            if f'cd "{root}"' in line
            or (line.startswith("# aiduMEI:") and index + 1 < len(lines)
                and f'cd "{root}"' in lines[index + 1])]


# ---------------------------------------------------------------------------
# O-3: identity is name + root
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shell", _SHELLS)
def test_sandbox_install_leaves_production_entries_untouched(shell, tmp_path):
    prod, sandbox = _checkout(tmp_path, "prod"), _checkout(tmp_path, "sandbox")
    fake = _fake_bin(tmp_path)
    state = tmp_path / "crontab.txt"
    prod_env, sandbox_env = _env(tmp_path, prod, fake), _env(tmp_path, sandbox, fake)

    assert _cron(shell, prod, prod_env, "install").returncode == 0
    prod_block = _lines_of(state, prod)
    expected = json.loads(_cron(shell, prod, prod_env, "--installed").stdout)["expected"]
    assert len(prod_block) == 2 * expected

    second = _cron(shell, sandbox, sandbox_env, "install")
    assert second.returncode == 0, second.stderr
    assert _lines_of(state, prod) == prod_block, "the sandbox moved production's entries"
    assert len(_lines_of(state, sandbox)) == 2 * expected
    assert "belongs to another checkout" in second.stderr

    prod_view = json.loads(_cron(shell, prod, prod_env, "--installed").stdout)
    sandbox_view = json.loads(_cron(shell, sandbox, sandbox_env, "--installed").stdout)
    for view, other in ((prod_view, sandbox), (sandbox_view, prod)):
        assert view["ok"] and view["installed"] == expected
        assert {item["root"] for item in view["foreign"]} == {str(other)}
        assert len(view["foreign"]) == expected


@pytest.mark.parametrize("shell", _SHELLS)
def test_own_stale_entry_is_still_replaced(shell, tmp_path):
    """Control: identity by root must not turn replacement off for our root."""
    prod = _checkout(tmp_path, "prod")
    fake = _fake_bin(tmp_path)
    state = tmp_path / "crontab.txt"
    env = _env(tmp_path, prod, fake)
    assert _cron(shell, prod, env, "install").returncode == 0
    text = state.read_text(encoding="utf-8")
    stale = text.replace("0 4 * * * cd", "9 9 * * * cd", 1)
    assert stale != text
    state.write_text(stale, encoding="utf-8")
    assert json.loads(_cron(shell, prod, env, "--installed").stdout)["tasks"][
        "consolidator"]["status"] == "drift"
    assert _cron(shell, prod, env, "install").returncode == 0
    assert state.read_text(encoding="utf-8") == text


# ---------------------------------------------------------------------------
# duplicate consolidator: unmanaged lines that run this root's scripts
# ---------------------------------------------------------------------------

def _legacy_lines(root: Path) -> dict[str, tuple[str, str, list[str]]]:
    py = f"{root}/venv/bin/python3"
    consolidator = ("scripts/consolidator.py", ["consolidator"])
    return {
        "runuser-absolute": (
            f"0 4 * * * runuser -u aidumem -- {py} {root}/scripts/consolidator.py "
            f">> {root}/logs/consolidator.log 2>&1", *consolidator),
        "cd-relative": (
            f"0 4 * * * cd {root} && runuser -u aidumem -- venv/bin/python "
            "scripts/consolidator.py", *consolidator),
        "nested-shell": (
            f"0 4 * * * /usr/sbin/runuser -u aidumem -- /bin/bash -c "
            f"'cd {root} && ./venv/bin/python scripts/consolidator.py'", *consolidator),
        "direct-exec": (
            f"30 2 * * * {root}/scripts/backup_gate.sh create daily",
            "scripts/backup_gate.sh", ["backup_create"]),
    }


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("shape", list(_legacy_lines(Path("/x"))))
def test_unmanaged_line_running_a_managed_script_is_drift(shell, tmp_path, shape):
    prod = _checkout(tmp_path, "prod")
    fake = _fake_bin(tmp_path)
    state = tmp_path / "crontab.txt"
    env = _env(tmp_path, prod, fake)
    assert _cron(shell, prod, env, "install").returncode == 0
    line, script, tasks = _legacy_lines(prod)[shape]
    state.write_text(line + "\n" + state.read_text(encoding="utf-8"), encoding="utf-8")

    checked = _cron(shell, prod, env, "--installed")
    data = json.loads(checked.stdout)
    assert not data["ok"]
    assert data["unmanaged"] == [{"line": 1, "script": script, "tasks": tasks}]
    assert data["tasks"][tasks[0]]["status"] == "drift"
    assert data["tasks"][tasks[0]]["unmanaged_lines"] == [1]
    assert "outside the managed block" in checked.stderr

    # install never deletes a line it does not own; it keeps reporting it.
    reinstall = _cron(shell, prod, env, "install")
    assert reinstall.returncode != 0
    assert state.read_text(encoding="utf-8").startswith(line + "\n")


@pytest.mark.parametrize("shell", _SHELLS)
def test_mentions_and_other_roots_are_not_unmanaged_drift(shell, tmp_path):
    """Negative controls with discriminating power for the scanner above."""
    prod = _checkout(tmp_path, "prod")
    other = tmp_path / "other"
    fake = _fake_bin(tmp_path)
    state = tmp_path / "crontab.txt"
    env = _env(tmp_path, prod, fake)
    assert _cron(shell, prod, env, "install").returncode == 0
    noise = [
        f"0 0 * * * /bin/echo {prod}/scripts/report.py",                 # a mention
        f"0 4 * * * runuser -u aidumem -- {other}/venv/bin/python3 "
        f"{other}/scripts/consolidator.py",                              # another root
        "MAILTO=ops@example.test",
        "# 0 4 * * * " + _legacy_lines(prod)["runuser-absolute"][0],     # commented out
    ]
    state.write_text("\n".join(noise) + "\n" + state.read_text(encoding="utf-8"),
                     encoding="utf-8")
    data = json.loads(_cron(shell, prod, env, "--installed").stdout)
    assert data["unmanaged"] == []
    assert data["ok"]


# ---------------------------------------------------------------------------
# optional run-as for the data-writing consolidator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("source", ["environment", "dotenv"])
def test_root_installer_demotes_the_consolidator(shell, tmp_path, source):
    prod = _checkout(tmp_path, "prod")
    fake = _fake_bin(tmp_path, root=True)
    state = tmp_path / "crontab.txt"
    extra = {"AIDUMEI_CRON_RUN_AS": "aidumem"} if source == "environment" else {}
    if source == "dotenv":
        (prod / ".env").write_text("export AIDUMEI_CRON_RUN_AS='aidumem'\n", encoding="utf-8")
    env = _env(tmp_path, prod, fake, **extra)
    installed = _cron(shell, prod, env, "install")
    assert installed.returncode == 0, installed.stderr
    lines = state.read_text(encoding="utf-8").splitlines()
    consolidator = lines[lines.index(next(x for x in lines
                                         if x.startswith("# aiduMEI:consolidator|"))) + 1]
    assert f'"{fake}/runuser" -u aidumem -- "{sys.executable}" scripts/consolidator.py' \
        in consolidator
    demoted = [x for x in lines if " -u aidumem -- " in x]
    assert demoted == [consolidator], "only the data-writing task is demoted"
    assert (tmp_path / "chown.log").read_text().split() == [
        "aidumem", str(prod / "logs/consolidator.log")]

    view = json.loads(_cron(shell, prod, env, "--installed").stdout)
    assert view["ok"] and view["tasks"]["consolidator"]["run_as"] == "aidumem"
    # A verifier that was not told the user (report.py's hourly call runs
    # without it) accepts the deliberate demotion instead of turning yellow.
    (prod / ".env").unlink(missing_ok=True)
    plain = _env(tmp_path, prod, _fake_bin(tmp_path / "plain"))
    tolerant = json.loads(_cron(shell, prod, plain, "--installed").stdout)
    assert tolerant["ok"] and tolerant["tasks"]["consolidator"]["run_as"] == "aidumem"


@pytest.mark.parametrize("shell", _SHELLS)
def test_reinstall_without_run_as_drops_it_loudly(shell, tmp_path):
    prod = _checkout(tmp_path, "prod")
    fake = _fake_bin(tmp_path, root=True)
    state = tmp_path / "crontab.txt"
    assert _cron(shell, prod, _env(tmp_path, prod, fake, AIDUMEI_CRON_RUN_AS="aidumem"),
                 "install").returncode == 0
    again = _cron(shell, prod, _env(tmp_path, prod, fake), "install")
    assert again.returncode == 0, again.stderr
    assert "consolidator ran as 'aidumem'" in again.stderr
    assert " -u aidumem -- " not in state.read_text(encoding="utf-8")


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("bad", ["Aidumem", "aid mem", "x;touch${IFS}pwned", "-u", "a" * 33,
                                 "$(id)", "../root"])
def test_invalid_run_as_names_are_refused_before_crontab_changes(shell, tmp_path, bad):
    prod = _checkout(tmp_path, "prod")
    fake = _fake_bin(tmp_path, root=True)
    state = tmp_path / "crontab.txt"
    state.write_text("MAILTO=ops@example.test\n", encoding="utf-8")
    result = _cron(shell, prod, _env(tmp_path, prod, fake, AIDUMEI_CRON_RUN_AS=bad), "install")
    assert result.returncode == 2
    assert "plain user name" in result.stderr
    assert state.read_text(encoding="utf-8") == "MAILTO=ops@example.test\n"
    assert not (tmp_path / "pwned").exists()


@pytest.mark.parametrize("shell", _SHELLS)
def test_run_as_needs_root_and_a_real_user(shell, tmp_path):
    prod = _checkout(tmp_path, "prod")
    state = tmp_path / "crontab.txt"
    # Not root: noted and ignored, no prefix.
    plain = _env(tmp_path, prod, _fake_bin(tmp_path / "plain"), AIDUMEI_CRON_RUN_AS="aidumem")
    result = _cron(shell, prod, plain, "install")
    assert result.returncode == 0, result.stderr
    assert "ignored: runuser needs a root installer" in result.stderr
    assert "runuser" not in state.read_text(encoding="utf-8")
    # Root, but no such user: refused before the crontab changes.
    before = state.read_text(encoding="utf-8")
    root = _env(tmp_path, prod, _fake_bin(tmp_path, root=True), AIDUMEI_CRON_RUN_AS="nosuchuser")
    refused = _cron(shell, prod, root, "install")
    assert refused.returncode == 1 and "no such user" in refused.stderr
    assert state.read_text(encoding="utf-8") == before
