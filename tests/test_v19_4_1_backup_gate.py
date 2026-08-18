"""
tests/test_v19_4_1_backup_gate.py — v19.4.1 备份门禁回归（B2 备份纪律真的能卡住）

⚠️ 这组测试来自生产实机的一次「门禁自相矛盾」现场：
    `backup_gate.sh create` 明明报「备份完成并通过校验」，
    紧接着 `backup_gate.sh require` 却判「没有任何通过 sha256 验证的备份
    ——拒绝迁移」，4 个 `.db-shm` 全部 FAILED。

    根因不是备份坏了，而是校验顺序错了：
      ① cp -a 连 `-wal`/`-shm` 一起拷
      ② 对所有文件算 SHA256SUMS
      ③ 再逐个 `.db` 跑 quick_check —— 打开库会重建 `-shm`、
         并把 `-wal` 的页 checkpoint 进主库，第 ② 步的基线当场失效。

    后果比「备份失败」更坏：硬门禁 100% 拦人，运维只会学会绕过它，
    B2 备份纪律从「卡入口」退化成「形同虚设」。

    修复：用 SQLite 在线备份 API 生成已合并 WAL 的单文件一致快照，
    备份目录不留伴生文件，sha256 最后算。
"""

import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_GATE = _REPO_ROOT / "scripts" / "backup_gate.sh"

pytestmark = pytest.mark.skipif(
    not _GATE.exists() or sys.platform.startswith("win"),
    reason="backup_gate.sh 不可用（需 POSIX shell）",
)


def _run_gate(args, *, data_dir=None, backup_root, expect_ok=True):
    env = dict(os.environ)
    env["AIDUMEM_BACKUP_ROOT"] = str(backup_root)
    if data_dir is not None:
        env["AIDUMEM_DATA_DIR"] = str(data_dir)
    proc = subprocess.run(
        ["bash", str(_GATE), *args],
        capture_output=True, text=True, env=env, cwd=str(_REPO_ROOT),
    )
    if expect_ok:
        assert proc.returncode == 0, f"gate {args} 失败:\n{proc.stdout}\n{proc.stderr}"
    return proc


def _make_wal_db(path, rows=300):
    """造一个处于 WAL 模式且有未 checkpoint 数据的库（模拟运行中的生产库）。"""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO facts(v) VALUES(?)", [(f"row{i}",) for i in range(rows)])
    conn.commit()
    return conn   # 刻意不关闭：-wal/-shm 保持在场


@pytest.fixture
def wal_data_dir(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    conn = _make_wal_db(data / "facts.db")
    (data / "state.json").write_text('{"k": "v"}', encoding="utf-8")
    sub = data / "qdrant"
    sub.mkdir()
    (sub / "meta.json").write_text("{}", encoding="utf-8")
    assert (data / "facts.db-wal").exists(), "前提：源库应处于 WAL 模式且未 checkpoint"
    yield data
    conn.close()


def _persistent_root(tmp_path):
    """backup_gate 铁律拒绝 /tmp 系备份根，这里造一个非 /tmp 的持久目录。"""
    root = pathlib.Path.home() / ".aidumem_test_backups" / tmp_path.name
    root.parent.mkdir(exist_ok=True)
    if root.exists():
        shutil.rmtree(root)
    return root


def test_create_then_require_is_self_consistent(wal_data_dir, tmp_path):
    """核心回归：create 报通过之后，require 必须立刻放行

    这正是生产实机自相矛盾的那一步。
    """
    root = _persistent_root(tmp_path)
    try:
        proc = _run_gate(["create", "regress"], data_dir=wal_data_dir, backup_root=root)
        assert "备份完成并通过校验" in proc.stdout

        req = _run_gate(["require"], backup_root=root)
        assert "硬门禁放行" in req.stdout, "create 通过但 require 拒绝 —— 门禁自相矛盾"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_backup_contains_no_wal_sidecars(wal_data_dir, tmp_path):
    """备份目录不得留 -wal/-shm：它们是校验和永久漂移的根源"""
    root = _persistent_root(tmp_path)
    try:
        _run_gate(["create", "nosidecar"], data_dir=wal_data_dir, backup_root=root)
        dest = next(root.glob("pre-nosidecar-*"))
        names = {p.name for p in dest.iterdir()}
        assert not any(n.endswith(("-wal", "-shm", "-journal")) for n in names), (
            f"备份留下了伴生文件: {sorted(names)}"
        )
        assert "facts.db" in names
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_snapshot_preserves_all_rows_including_unflushed_wal(wal_data_dir, tmp_path):
    """一致快照必须包含尚在 WAL 里、未 checkpoint 的数据（零丢失）"""
    root = _persistent_root(tmp_path)
    try:
        _run_gate(["create", "rows"], data_dir=wal_data_dir, backup_root=root)
        dest = next(root.glob("pre-rows-*"))
        conn = sqlite3.connect(f"file:{dest / 'facts.db'}?mode=ro", uri=True)
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 300
        conn.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_non_db_files_and_subdirs_are_copied(wal_data_dir, tmp_path):
    """非数据库文件与子目录（如 qdrant/）不能在改造中被漏掉"""
    root = _persistent_root(tmp_path)
    try:
        _run_gate(["create", "misc"], data_dir=wal_data_dir, backup_root=root)
        dest = next(root.glob("pre-misc-*"))
        assert (dest / "state.json").read_text(encoding="utf-8") == '{"k": "v"}'
        assert (dest / "qdrant" / "meta.json").exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_verify_does_not_invalidate_checksums(wal_data_dir, tmp_path):
    """verify 跑完 quick_check 后，require 仍须放行

    「校验动作本身不得破坏校验基线」—— 这是本次修复的核心不变量。
    """
    root = _persistent_root(tmp_path)
    try:
        _run_gate(["create", "verifyloop"], data_dir=wal_data_dir, backup_root=root)
        dest = next(root.glob("pre-verifyloop-*"))

        v = _run_gate(["verify", str(dest)], backup_root=root)
        assert "sha256 全部匹配" in v.stdout

        req = _run_gate(["require"], backup_root=root)
        assert "硬门禁放行" in req.stdout, "verify 之后 require 被拒 —— 校验动作打废了基线"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_require_rejects_when_no_backup(tmp_path):
    """无备份时门禁必须拦住（门禁本身不能被改坏）"""
    root = _persistent_root(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    try:
        proc = _run_gate(["require"], backup_root=root, expect_ok=False)
        assert proc.returncode == 1
        assert "拒绝迁移" in proc.stderr + proc.stdout
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_tmp_backup_root_is_refused(wal_data_dir):
    """备份根在 /tmp 系一律拒绝（重启即灰飞烟灭，v19.4.0 铁律）"""
    proc = _run_gate(
        ["create", "tmproot"],
        data_dir=wal_data_dir,
        backup_root="/tmp/aidumem_should_be_refused",
        expect_ok=False,
    )
    assert proc.returncode == 1
    assert "铁律拒绝" in proc.stdout + proc.stderr
