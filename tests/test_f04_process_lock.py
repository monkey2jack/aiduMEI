"""Real process exclusion, alias paths and lock release on process death."""
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CODE = """
import sys
from ducky.process_lock import acquire_api_process_lock
acquire_api_process_lock(sys.argv[1])
acquire_api_process_lock(sys.argv[1])
print('owned', flush=True)
if len(sys.argv) > 2:
    sys.stdin.read()
"""


def test_real_second_process_denied_then_process_exit_releases(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    directory = tmp_path / "store"
    directory.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    first = subprocess.Popen([sys.executable, "-c", CODE, str(directory), "hold"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env)
    try:
        assert first.stdout.readline().strip() == "owned"
        for target in (directory, alias):
            second = subprocess.run([sys.executable, "-c", CODE, str(target)],
                                    capture_output=True, text=True, env=env, timeout=10)
            assert second.returncode != 0
            assert "another API process holds" in second.stderr
        independent = subprocess.run([sys.executable, "-c", CODE, str(tmp_path / "other")],
                                     capture_output=True, text=True, env=env, timeout=10)
        assert independent.returncode == 0, independent.stderr
    finally:
        first.kill()
        first.communicate(timeout=10)
    after = subprocess.run([sys.executable, "-c", CODE, str(directory)],
                           capture_output=True, text=True, env=env, timeout=10)
    assert after.returncode == 0, after.stderr
