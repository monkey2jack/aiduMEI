"""f0.3: no shell script may put the bearer token on curl's command line.

Command-line arguments are visible to every local user through `ps`.  The
supported pattern is a 0600 header file (`-H @file`) or stdin (`-H @-`).
This guard scans every tracked shell script and also executes the header-file
snippet of one script under bash to prove the file is private and complete.
"""
import os
import pathlib
import re
import stat
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
ARGV_TOKEN = re.compile(r'-H\s+"Authorization:\s*Bearer\s+\$\{?AIDUMEM_API_TOKEN')


def _shell_scripts():
    out = subprocess.run(["git", "ls-files", "*.sh"], cwd=ROOT, capture_output=True, text=True)
    files = [ROOT / p for p in out.stdout.split()] if out.returncode == 0 else list(ROOT.rglob("*.sh"))
    return [p for p in files if ".venv" not in p.parts and p.is_file()]


def test_no_shell_script_puts_bearer_token_on_argv():
    scripts = _shell_scripts()
    assert len(scripts) >= 10, "scan range collapsed"
    offenders = [str(p.relative_to(ROOT)) for p in scripts if ARGV_TOKEN.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert offenders == [], f"token on curl argv: {offenders}"


def test_negative_control_pattern_catches_the_old_form():
    old = 'AUTH_ARGS=(-H "Authorization: Bearer ${AIDUMEM_API_TOKEN}")'
    assert ARGV_TOKEN.search(old)


def test_header_file_snippet_is_private(tmp_path):
    src = (ROOT / "scripts" / "post-upgrade-check.sh").read_text(encoding="utf-8")
    start = src.index("AUTH_ARGS=()")
    end = src.index("fi", start) + 2
    snippet = src[start:end] + '\nprintf "%s\\n" "${AUTH_ARGS[@]}"\nstat_file="${_AUTH_HDR_FILE}"\ncp "$stat_file" "$OUT/hdr"\nls -l "$stat_file" | cut -c1-10 > "$OUT/mode"\n'
    env = dict(os.environ, AIDUMEM_API_TOKEN="tok-123", TMPDIR=str(tmp_path), OUT=str(tmp_path))
    res = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.split()
    assert lines[0] == "-H" and lines[1].startswith("@") and "tok-123" not in res.stdout
    assert (tmp_path / "hdr").read_text() == "Authorization: Bearer tok-123\n"
    assert (tmp_path / "mode").read_text().startswith("-rw-------")
