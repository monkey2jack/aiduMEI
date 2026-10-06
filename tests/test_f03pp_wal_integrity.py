"""f0.3++: WAL corruption, I/O, fail-closed locks and real process races.

Windows locking is simulated here; POSIX locks and directory fsync run on
the host.  Every engine and child process uses an isolated temporary ledger.
"""
from __future__ import annotations

import builtins
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import textwrap
import types
from unittest.mock import Mock

import pytest

import ducky.wal_engine as we


@pytest.fixture()
def wal(tmp_path):
    return we.WALEngine(wal_dir=str(tmp_path / "wal"))


def _entry(wal_id):
    return we.WALEntry(
        wal_id=wal_id, timestamp=1, operation="delete", user_id="u1",
        payload={"memory_id": wal_id, "bank_id": "default"},
    )


def _use_for_startup(wal, monkeypatch):
    monkeypatch.setattr(we.WALEngine, "get_instance", classmethod(lambda cls: wal))
    cascade = Mock(side_effect=AssertionError("unknown WAL must not replay"))
    finish = Mock(side_effect=AssertionError("unknown WAL must not compact or spawn replay"))
    monkeypatch.setattr(we, "cascade_delete_memory", cascade)
    monkeypatch.setattr(we, "_finish_reconcile", finish)
    return cascade, finish


def _assert_unknown(report, *, pending_count=None):
    assert report["wal_integrity"] == "unknown"
    assert report["wal_integrity_error"]
    assert report["reconciliation_paused"] is True
    assert report["pending_count"] == pending_count
    assert report["recovered"] == report["failed"] == 0


@pytest.mark.parametrize("bad_row", [b"not-json\n", b'{"wal_id":"torn","operation":'],
                         ids=["bad-row", "torn-tail"])
def test_bad_rows_fail_reads_survive_compact_and_pause_startup(wal, monkeypatch, bad_row):
    wal.append(_entry("pending-before"))
    wal.append(_entry("settled"))
    wal.mark_status("settled", "committed")
    with open(wal.wal_file, "ab") as handle:
        handle.write(bad_row)
    if bad_row.endswith(b"\n"):
        wal.append(_entry("pending-after"))

    with pytest.raises(we.WALIntegrityError, match="行无法解析"):
        wal.get_pending_entries()
    report = wal.compact(keep_recent_seconds=0)
    assert report["unparsable_kept"] == 1
    assert report["dropped"] == 1
    expected = {"pending-before", "pending-after"} if bad_row.endswith(b"\n") else {"pending-before"}
    assert report["kept"] == len(expected)
    compacted = wal.wal_file.read_bytes()
    assert compacted.endswith(bad_row.rstrip(b"\n") + b"\n")
    valid_rows = [we.WALEntry.from_json(line) for line in compacted.decode().splitlines()]
    assert {entry.wal_id for entry in valid_rows if entry} == expected
    reopened = we.WALEngine(str(wal.wal_dir))
    with pytest.raises(we.WALIntegrityError):
        reopened.get_pending_entries()

    cascade, finish = _use_for_startup(reopened, monkeypatch)
    _assert_unknown(we.reconcile_startup())
    cascade.assert_not_called()
    finish.assert_not_called()
    assert reopened.wal_file.read_bytes() == compacted


@pytest.mark.parametrize("operation", ["get_pending_entries", "compact"])
@pytest.mark.parametrize("failure", ["open", "iteration", "invalid-utf8"])
def test_read_failures_never_return_empty_or_replace_ledger(wal, monkeypatch, operation, failure):
    wal.append(_entry("must-survive-read-error"))
    if failure == "invalid-utf8":
        with open(wal.wal_file, "ab") as handle:
            handle.write(b"\xff\xfe\n")
    before = wal.wal_file.read_bytes()
    real_open = builtins.open

    class BrokenReader:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            yield _entry("partial-read").to_json() + "\n"
            raise OSError("injected read iteration failure")

    def injected_open(path, mode="r", *args, **kwargs):
        if Path(path) == wal.wal_file and "r" in mode:
            if failure == "open":
                raise PermissionError("injected WAL open failure")
            if failure == "iteration":
                return BrokenReader()
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", injected_open)
    replace = Mock(side_effect=AssertionError("unreadable WAL must not be replaced"))
    monkeypatch.setattr(we.os, "replace", replace)
    with pytest.raises(we.WALIntegrityError) as caught:
        getattr(wal, operation)()
    assert isinstance(caught.value.__cause__, (OSError, UnicodeError))
    assert wal.wal_file.read_bytes() == before
    replace.assert_not_called()
    cascade, finish = _use_for_startup(wal, monkeypatch)
    _assert_unknown(we.reconcile_startup())
    cascade.assert_not_called()
    finish.assert_not_called()


@pytest.mark.parametrize("operation", ["get_pending_entries", "compact"])
def test_stat_failure_is_unknown_instead_of_missing_file(wal, monkeypatch, operation):
    wal.append(_entry("stat-error"))
    real_stat = Path.stat

    def fail_wal_stat(path, *args, **kwargs):
        if path == wal.wal_file:
            raise PermissionError("injected WAL stat failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_wal_stat)
    with pytest.raises(we.WALIntegrityError):
        getattr(wal, operation)()
    _use_for_startup(wal, monkeypatch)
    _assert_unknown(we.reconcile_startup())


@pytest.mark.parametrize("operation", ["get_pending_entries", "compact", "append"])
def test_sidecar_open_failure_is_fail_closed_even_for_missing_wal(wal, monkeypatch, operation):
    wal.lock_file.mkdir()
    with pytest.raises(we.WALIntegrityError):
        if operation == "append":
            wal.append(_entry("must-not-be-written"))
        else:
            getattr(wal, operation)()
    assert not wal.wal_file.exists()
    _use_for_startup(wal, monkeypatch)
    _assert_unknown(we.reconcile_startup())


@pytest.mark.parametrize("operation", ["get_pending_entries", "compact", "append"])
def test_simulated_windows_acquisition_error_never_runs_operation(wal, monkeypatch, operation):
    """Runs on the host with a fake msvcrt, not a Windows runtime."""
    wal.append(_entry("existing-pending"))
    before = wal.wal_file.read_bytes()
    calls = []

    def locking(fd, mode, count):
        calls.append(mode)
        assert count == 1
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        assert os.fstat(fd).st_size >= 1
        raise OSError("simulated Windows lock acquisition failure")

    monkeypatch.setattr(we.platform, "system", lambda: "Windows")
    monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(LK_LOCK=1, LK_UNLCK=2, locking=locking))
    with pytest.raises(we.WALIntegrityError, match="simulated Windows lock acquisition failure"):
        if operation == "append":
            wal.append(_entry("forbidden-pending"))
        else:
            getattr(wal, operation)()
    assert calls == [1], "failed acquisition must not yield or try to unlock"
    assert wal.wal_file.read_bytes() == before
    _use_for_startup(wal, monkeypatch)
    _assert_unknown(we.reconcile_startup())


def test_simulated_windows_unlock_error_is_unknown(wal, monkeypatch):
    wal.append(_entry("unlock-error"))
    before = wal.wal_file.read_bytes()
    calls = []

    def locking(fd, mode, count):
        calls.append(mode)
        if mode == 2:
            raise OSError("simulated Windows unlock failure")

    monkeypatch.setattr(we.platform, "system", lambda: "Windows")
    monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(LK_LOCK=1, LK_UNLCK=2, locking=locking))
    with pytest.raises(we.WALIntegrityError, match="simulated Windows unlock failure"):
        wal.get_pending_entries()
    assert calls == [1, 2]
    _use_for_startup(wal, monkeypatch)
    _assert_unknown(we.reconcile_startup())
    assert wal.wal_file.read_bytes() == before


@pytest.mark.skipif(os.name == "nt", reason="actual POSIX flock path")
@pytest.mark.parametrize("failure", ["acquire", "release"])
def test_posix_lock_errors_pause_startup(wal, monkeypatch, failure):
    import fcntl

    wal.append(_entry("posix-lock-error"))
    real_flock = fcntl.flock

    def fail_flock(fd, mode):
        if (mode == fcntl.LOCK_UN) == (failure == "release"):
            raise OSError("injected POSIX flock failure")
        return real_flock(fd, mode)

    monkeypatch.setattr(fcntl, "flock", fail_flock)
    with pytest.raises(we.WALIntegrityError, match="POSIX flock failure"):
        wal.get_pending_entries()
    _use_for_startup(wal, monkeypatch)
    _assert_unknown(we.reconcile_startup())


def test_genuinely_missing_and_blank_wal_remain_empty(wal):
    assert wal.get_pending_entries() == []
    assert wal.compact()["kept"] == 0
    wal.wal_file.write_text("\n  \n\t\n", encoding="utf-8")
    assert wal.get_pending_entries() == []
    assert wal.compact()["unparsable_kept"] == 0


@pytest.mark.parametrize("user_id", ["default", "configured-owner", "named-tenant"])
@pytest.mark.parametrize("confirm_kwargs", [{}, {"confirm": False}], ids=["omitted", "false"])
def test_delete_all_requires_confirmation_for_every_user(wal, monkeypatch, user_id, confirm_kwargs):
    wal.append(_entry("confirmation-must-preserve-this"))
    before = wal.wal_file.read_bytes()
    monkeypatch.setattr(we, "DEFAULT_USER_ID", "configured-owner")
    get_wal = Mock(side_effect=AssertionError("refusal must precede WAL and delete side effects"))
    monkeypatch.setattr(we.WALEngine, "get_instance", get_wal)

    with pytest.raises(ValueError, match="必须传递 confirm=True"):
        we.cascade_delete_all(user_id=user_id, bank_id="work", **confirm_kwargs)

    get_wal.assert_not_called()
    assert wal.wal_file.read_bytes() == before


@pytest.mark.skipif(os.name == "nt", reason="actual POSIX directory fsync")
def test_compact_persists_replace_before_unlocking(wal, monkeypatch):
    import fcntl

    wal.append(_entry("durable-pending"))
    events = []
    real_fsync, real_replace, real_flock, real_close = os.fsync, os.replace, fcntl.flock, os.close

    def fsync(fd):
        events.append("directory-fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-fsync")
        return real_fsync(fd)

    def replace(src, dst):
        result = real_replace(src, dst)
        events.append("replace")
        return result

    def flock(fd, mode):
        if mode == fcntl.LOCK_UN:
            events.append("unlock")
        return real_flock(fd, mode)

    def close(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            events.append("directory-close")
        return real_close(fd)

    monkeypatch.setattr(we.os, "fsync", fsync)
    monkeypatch.setattr(we.os, "replace", replace)
    monkeypatch.setattr(we.os, "close", close)
    monkeypatch.setattr(fcntl, "flock", flock)
    report = wal.compact(keep_recent_seconds=0)
    assert report["kept"] == 1
    assert events == ["file-fsync", "replace", "directory-fsync", "directory-close", "unlock"]
    assert [entry.wal_id for entry in we.WALEngine(str(wal.wal_dir)).get_pending_entries()] == ["durable-pending"]


@pytest.mark.skipif(os.name == "nt", reason="actual POSIX directory fsync")
@pytest.mark.parametrize("failure", ["directory-open", "directory-fsync"])
def test_directory_durability_error_keeps_pending_and_startup_unknown(wal, monkeypatch, failure):
    wal.append(_entry("durability-unknown"))
    wal.mark_status("durability-unknown", "committed")
    wal.append(_entry("still-pending"))
    real_open, real_fsync, real_close = os.open, os.fsync, os.close
    directory_fds = []
    closed_fds = []

    def directory_open(path, flags, *args, **kwargs):
        if Path(path) == wal.wal_dir:
            if failure == "directory-open":
                raise OSError("injected directory open failure")
            fd = real_open(path, flags, *args, **kwargs)
            directory_fds.append(fd)
            return fd
        return real_open(path, flags, *args, **kwargs)

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected directory fsync failure")
        return real_fsync(fd)

    def close(fd):
        closed_fds.append(fd)
        return real_close(fd)

    monkeypatch.setattr(we.os, "open", directory_open)
    monkeypatch.setattr(we.os, "fsync", fsync)
    monkeypatch.setattr(we.os, "close", close)
    with pytest.raises(we.WALIntegrityError, match="injected directory"):
        wal.compact(keep_recent_seconds=0)
    assert wal.compactions == 0
    assert directory_fds == closed_fds
    assert not wal.wal_file.with_suffix(".wal.tmp").exists()
    assert [entry.wal_id for entry in wal.get_pending_entries()] == ["still-pending"]
    monkeypatch.setattr(we.WALEngine, "get_instance", classmethod(lambda cls: wal))
    monkeypatch.setattr(we, "cascade_delete_memory", Mock(return_value={"status": "committed"}))
    report = we.reconcile_startup()
    assert report["wal_integrity"] == "unknown"
    assert report["reconciliation_paused"] is True
    assert "error" in report["compacted"]
    assert "pending_replay_spawned" not in report


def test_startup_status_write_lock_failure_never_claims_recovery(wal, monkeypatch):
    wal.append(_entry("unresolved-delete"))
    before = wal.wal_file.read_bytes()
    monkeypatch.setattr(we.WALEngine, "get_instance", classmethod(lambda cls: wal))

    def lose_lock(*args, **kwargs):
        wal.lock_file.unlink()
        wal.lock_file.mkdir()
        return {"status": "committed"}

    monkeypatch.setattr(we, "cascade_delete_memory", lose_lock)
    report = we.reconcile_startup()
    _assert_unknown(report, pending_count=1)
    assert wal.wal_file.read_bytes() == before


def _start_child(wal, script, *args):
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-u", "-c", textwrap.dedent(script), str(wal.wal_dir), *map(str, args)],
        cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _read_line(process, timeout=10):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout), "child did not reach its synchronization point"
    line = process.stdout.readline().strip()
    assert line, f"child exited early with code {process.poll()}"
    return line


def _release(process):
    process.stdin.write("continue\n")
    process.stdin.flush()


def _assert_child_succeeded(process):
    out, err = process.communicate(timeout=20)
    assert process.returncode == 0, f"child failed: {out}\n{err}"


def _stop_children(processes):
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.skipif(os.name == "nt", reason="actual independent POSIX process locking")
def test_independent_appenders_block_at_compact_replace_window(wal):
    """Hold a real compactor after its snapshot; independent writers must wait."""
    wal.append(_entry("original-pending"))
    before = wal.wal_file.read_bytes()
    compactor = _start_child(wal, '''
        import os, sys
        import ducky.wal_engine as we
        wal = we.WALEngine(sys.argv[1])
        real_replace = os.replace
        def gated_replace(src, dst):
            print("snapshot-ready", flush=True)
            input()
            return real_replace(src, dst)
        we.os.replace = gated_replace
        wal.compact(keep_recent_seconds=0)
        print("compacted", flush=True)
    ''')
    children = [compactor]
    try:
        assert _read_line(compactor) == "snapshot-ready"
        for worker in range(3):
            writer = _start_child(wal, '''
                import os, sys
                from ducky.wal_engine import WALEngine, WALEntry
                wal = WALEngine(sys.argv[1])
                print("attempt:" + str(os.getpid()), flush=True)
                wid = "writer-" + sys.argv[2]
                wal.append(WALEntry(wal_id=wid, payload={"worker": sys.argv[2]}))
                print("appended", flush=True)
            ''', worker)
            children.append(writer)
            assert _read_line(writer) == f"attempt:{writer.pid}"
        with selectors.DefaultSelector() as selector:
            for writer in children[1:]:
                selector.register(writer.stdout, selectors.EVENT_READ)
            assert not selector.select(0.25), "append crossed a compactor's sidecar lock"
        assert wal.wal_file.read_bytes() == before
        _release(compactor)
        for child in children:
            _assert_child_succeeded(child)
        reopened = we.WALEngine(str(wal.wal_dir))
        pending = reopened.get_pending_entries()
        assert {entry.wal_id for entry in pending} == {"original-pending", "writer-0", "writer-1", "writer-2"}
        assert len(pending) == 4
        assert {entry.payload["worker"] for entry in pending if entry.wal_id.startswith("writer-")} == {"0", "1", "2"}
    finally:
        _stop_children(children)


@pytest.mark.skipif(os.name == "nt", reason="actual independent POSIX process locking")
def test_multiple_processes_append_status_and_compact_without_losing_pending(wal):
    children = []
    try:
        for worker in range(3):
            children.append(_start_child(wal, '''
                import os, sys, time
                from ducky.wal_engine import WALEngine, WALEntry
                wal = WALEngine(sys.argv[1])
                print("ready:" + str(os.getpid()), flush=True)
                input()
                for i in range(36):
                    wid = f"worker-{sys.argv[2]}-{i}"
                    wal.append(WALEntry(wal_id=wid, timestamp=1, payload={"worker": sys.argv[2], "index": i}))
                    if i % 3 == 0:
                        wal.mark_status(wid, "committed")
                    time.sleep(0.001)
            ''', worker))
        for _ in range(2):
            children.append(_start_child(wal, '''
                import os, sys, time
                from ducky.wal_engine import WALEngine
                wal = WALEngine(sys.argv[1])
                print("ready:" + str(os.getpid()), flush=True)
                input()
                for _ in range(48):
                    wal.compact(keep_recent_seconds=0)
                    time.sleep(0.001)
            '''))
        for child in children:
            assert _read_line(child) == f"ready:{child.pid}"
        assert len({child.pid for child in children}) == 5
        for child in children:
            _release(child)
        for child in children:
            _assert_child_succeeded(child)
        reopened = we.WALEngine(str(wal.wal_dir))
        report = reopened.compact(keep_recent_seconds=0)
        pending = reopened.get_pending_entries()
        expected = {f"worker-{worker}-{i}" for worker in range(3) for i in range(36) if i % 3}
        assert {entry.wal_id for entry in pending} == expected
        assert len(pending) == report["kept"] == 72
        assert report["unparsable_kept"] == 0
        assert all(entry.payload == {"worker": entry.wal_id.split("-")[1], "index": int(entry.wal_id.split("-")[2])}
                   for entry in pending)
        assert {json.loads(line)["wal_id"] for line in reopened.wal_file.read_text().splitlines()} == expected
    finally:
        _stop_children(children)
