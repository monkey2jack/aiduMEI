"""f0.3 A3 -- consolidator writes every log line exactly once.

Production fact: logging.basicConfig installed FileHandler(consolidator.log) AND
StreamHandler(stderr), while cron runs the script with
`>> logs/consolidator.log 2>&1` -- stderr IS that same file, so every line was
written twice (consolidator.log grew 46-78 MB/day).

Fix under test: FileHandler always; StreamHandler only when the stream is a TTY.
The negative controls rebuild the old handler pair against the same
cron-shaped redirect and show the duplication is real.
"""
from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.consolidator as cons

ROOT = Path(__file__).resolve().parents[1]
LOGGER = "aiduMEM.consolidator"


class _FakeTty(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def root_logger():
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    yield root
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


def _count(path: Path, needle: str) -> int:
    return path.read_text(encoding="utf-8").count(needle) if path.exists() else 0


def _flush(handlers) -> None:
    for handler in handlers:
        handler.flush()


def test_non_tty_stream_gets_the_file_handler_only(tmp_path, root_logger):
    stream = io.StringIO()
    handlers = cons.configure_logging(stream=stream, log_dir=str(tmp_path))
    assert [type(h) for h in handlers] == [logging.FileHandler]
    logging.getLogger(LOGGER).info("probe-non-tty")
    _flush(handlers)
    assert _count(tmp_path / "consolidator.log", "probe-non-tty") == 1
    assert stream.getvalue() == ""


def test_tty_stream_also_gets_a_console_handler(tmp_path, root_logger):
    tty = _FakeTty()
    handlers = cons.configure_logging(stream=tty, log_dir=str(tmp_path))
    assert {type(h) for h in handlers} == {logging.FileHandler, logging.StreamHandler}
    logging.getLogger(LOGGER).info("probe-tty")
    _flush(handlers)
    assert _count(tmp_path / "consolidator.log", "probe-tty") == 1
    assert tty.getvalue().count("probe-tty") == 1


def test_cron_shaped_redirect_writes_each_line_once(tmp_path, root_logger):
    log = tmp_path / "consolidator.log"
    with open(log, "a", encoding="utf-8") as cron_stderr:   # `>> consolidator.log 2>&1`
        handlers = cons.configure_logging(stream=cron_stderr, log_dir=str(tmp_path))
        logging.getLogger(LOGGER).info("probe-cron-once")
        _flush(handlers)
        cron_stderr.flush()
    assert _count(log, "probe-cron-once") == 1


def test_negative_control_old_handler_pair_duplicates_every_line(tmp_path, root_logger):
    log = tmp_path / "consolidator.log"
    with open(log, "a", encoding="utf-8") as cron_stderr:
        old = [logging.FileHandler(str(log)), logging.StreamHandler(cron_stderr)]
        for handler in old:
            root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)
        logging.getLogger(LOGGER).info("probe-cron-twice")
        _flush(old)
    assert _count(log, "probe-cron-twice") == 2


def test_reconfiguring_does_not_stack_handlers(tmp_path, root_logger):
    cons.configure_logging(stream=io.StringIO(), log_dir=str(tmp_path))
    cons.configure_logging(stream=io.StringIO(), log_dir=str(tmp_path))
    tagged = [h for h in root_logger.handlers if getattr(h, cons._HANDLER_TAG, False)]
    assert len(tagged) == 1
    logging.getLogger(LOGGER).info("probe-reconfigure")
    _flush(tagged)
    assert _count(tmp_path / "consolidator.log", "probe-reconfigure") == 1


def _run_like_cron(tmp_path: Path, code: str) -> Path:
    log = tmp_path / "consolidator.log"
    env = {**os.environ, "PYTHONPATH": str(ROOT), "AIDUMEM_LOG_DIR": str(tmp_path)}
    with open(log, "ab") as fh:   # stdout and stderr both appended to the log file
        subprocess.run([sys.executable, "-c", code], stdout=fh, stderr=fh, cwd=str(ROOT),
                       env=env, timeout=120, check=True)
    return log


def test_real_process_with_stderr_redirected_like_cron(tmp_path):
    code = ("import logging, scripts.consolidator as c; c.configure_logging(); "
            f"logging.getLogger({LOGGER!r}).info('probe-subprocess-once')")
    assert _count(_run_like_cron(tmp_path, code), "probe-subprocess-once") == 1


def test_negative_control_real_process_with_the_old_basic_config(tmp_path):
    code = ("import logging, os, sys; logging.basicConfig(level=logging.INFO, handlers=["
            "logging.FileHandler(os.path.join(os.environ['AIDUMEM_LOG_DIR'], 'consolidator.log')), "
            "logging.StreamHandler(sys.stderr)]); "
            f"logging.getLogger({LOGGER!r}).info('probe-subprocess-twice')")
    assert _count(_run_like_cron(tmp_path, code), "probe-subprocess-twice") == 2


def test_importing_the_module_does_not_configure_logging(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "AIDUMEM_LOG_DIR": str(tmp_path)}
    code = "import logging, scripts.consolidator; print(len(logging.getLogger().handlers))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(ROOT), env=env, timeout=120, check=True)
    assert out.stdout.strip().splitlines()[-1] == "0"
    assert not (tmp_path / "consolidator.log").exists()
