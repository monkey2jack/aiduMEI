"""aiduMEI v20.0 — 测试套件数据目录隔离护栏

盯的是一个**沉默**的缺陷：套件在一棵已部署的树里跑，会把测试行写进那棵树的
生产库。它沉默是因为——**写进去的样子和没写进去的样子，在退出码上一模一样**，
都是绿的。v20.0 验收当天就这么中过一次：生产 `data/workspace.db` 里多出三条
`alice` 测试行，`data/qdrant/` 下多出一个没人持有的 `.lock`，而套件报的是
760 passed。

所以这里不测业务，只测**护栏本身还在不在、还灵不灵**：

  ① `conftest.py` 在位，且 `AIDUMEM_DATA_DIR` 真的被改指到临时目录；
  ② `ducky.utils.DATA_DIR` 落在仓库之外（这一条是"不写生产"的充要位置）；
  ③ workspace.db 跟着 DATA_DIR 走，不许自己硬拼回仓库；
  ④ 逃生门是**唯一**不隔离的路，且必须显式打开（负向对照）；
  ⑤ 逃生门不许变成默认（把它写成"默认打开"，这条会红）；
  ⑥ 清理只删自己建的那一个——子进程不许删父进程正在用的目录（真子进程对照）。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_test_data_isolation.py -v
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

_CONFTEST_PATH = os.path.join(_REPO, "conftest.py")


def _load_conftest():
    """拿到**正在生效的那一份**根 conftest，用来直接检查它的行为。

    优先从 ``sys.modules["conftest"]`` 取——pytest 就是用这个名字导入仓库根
    conftest 的。**不能**图省事另 exec 一份副本：副本会把 ``_redirect`` 再跑一遍，
    而 ``ducky.utils.DATA_DIR`` 早已冻结在第一次的目录上，于是"环境变量指一处、
    代码写另一处"，用例单跑绿、全量跑红。这个坑是写这条护栏时自己踩出来的。

    取不到才退化为加载副本（例如换了 rootdir 单跑本文件）。加载失败也**不许**
    炸在收集期：collection ERROR 会被读成"这个测试文件坏了"，而真相是
    "护栏不见了"——坏掉的样子要长得像坏掉，不能长得像另一回事。
    """
    live = sys.modules.get("conftest")
    if live is not None and getattr(live, "ESCAPE_HATCH", None) and \
            os.path.realpath(getattr(live, "__file__", "")) == \
            os.path.realpath(_CONFTEST_PATH):
        return live

    if not os.path.exists(_CONFTEST_PATH):
        return None
    spec = importlib.util.spec_from_file_location(
        "_aidumei_root_conftest", _CONFTEST_PATH)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 — 坏掉的护栏等于没护栏，交给用例报
        return None
    return module


_conftest = _load_conftest()


def _inside_repo(path: str) -> bool:
    return os.path.realpath(path).startswith(os.path.realpath(_REPO) + os.sep)


def _guard() -> object:
    """取根 conftest 模块；缺失就以一条读得懂的断言失败收场。"""
    assert _conftest is not None, (
        f"仓库根的 conftest.py 缺失或无法加载（{_CONFTEST_PATH}）——"
        "整套数据隔离失去立足点，在部署树里跑 pytest 会写进用户的生产库"
    )
    return _conftest


def _redirect_into(env: dict) -> str | None:
    """调一次 `_redirect`，并保证它建出来的临时目录不留在磁盘上。

    护栏自己的临时目录由 conftest 的 atexit 收尾；测试里为了对照多调的那几次
    得自己擦干净，不然验收机上会攒下一地空目录。**只**擦本次新建的那个：
    沿用分支返回的是会话正在用的目录，删它等于把套件后半程掀翻。
    """
    path, created = _guard()._redirect(env)
    if path and created:
        shutil.rmtree(path, ignore_errors=True)
    return path


# ═══════════════ ① 护栏在位 ═══════════════
def test_root_conftest_exists_and_redirected_the_env():
    """conftest.py 必须在仓库根，且已经把环境变量改指走。

    位置本身是语义：pytest 只保证在收集测试模块**之前**导入仓库根的
    conftest.py。挪到 tests/ 下也能跑，但 `ducky` 已经被别的模块 import 过
    的可能性就回来了——DATA_DIR 一旦定型就改不动了。
    """
    assert os.path.exists(os.path.join(_REPO, "conftest.py")), \
        "仓库根的 conftest.py 不见了——整套数据隔离失去立足点"

    env_dir = os.environ.get("AIDUMEM_DATA_DIR")
    assert env_dir, "AIDUMEM_DATA_DIR 未被设置——护栏没生效"
    assert not _inside_repo(env_dir), f"AIDUMEM_DATA_DIR 指在仓库内：{env_dir}"
    assert os.path.basename(env_dir).startswith(_guard().DIR_PREFIX), \
        "AIDUMEM_DATA_DIR 不是本套件建的临时目录——来路不明就不能算隔离"

    log_dir = os.environ.get("AIDUMEM_LOG_DIR")
    assert log_dir and not _inside_repo(log_dir), \
        f"AIDUMEM_LOG_DIR 仍指在仓库内（{log_dir}）——数据不写你的了、日志还写"


# ═══════════════ ② DATA_DIR 落在仓库之外 ═══════════════
def test_ducky_data_dir_resolves_outside_the_repo():
    """`ducky.utils.DATA_DIR` 是所有落盘路径的根，它出了仓库，测试就写不进生产。"""
    import ducky.utils as utils

    assert not _inside_repo(utils.DATA_DIR), (
        f"ducky.utils.DATA_DIR 仍在仓库内（{utils.DATA_DIR}）——"
        "在部署树里跑 pytest 会写进用户的生产库"
    )
    chosen = _guard().REDIRECTED_DATA_DIR
    assert chosen, "护栏没选定隔离目录——逃生门被默认打开了？"
    assert os.path.realpath(utils.DATA_DIR) == os.path.realpath(chosen), (
        f"DATA_DIR（{utils.DATA_DIR}）与护栏选定的目录（{chosen}）走散了——"
        "说明它没在 import 时读到护栏设的值"
    )


# ═══════════════ ③ workspace.db 跟着 DATA_DIR 走 ═══════════════
def test_workspace_db_follows_data_dir_and_never_hardwires_the_repo():
    """workspace.db 就是 v20.0 那次污染的落点，单独钉一遍。

    历史上它硬拼过 `ducky/data/`，绕开了 AIDUMEM_DATA_DIR 约定；
    这条测试是那次修复的看门人——改回硬拼，它先红。
    """
    checked = []
    for name in ("ducky.memory_workspace", "ducky.pipeline.memory_workspace"):
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        db = getattr(mod, "WORKSPACE_DB", None)
        if db is None:
            continue
        checked.append(name)
        assert not _inside_repo(db), f"{name}.WORKSPACE_DB 指在仓库内：{db}"
        assert os.path.realpath(os.path.dirname(db)) == \
            os.path.realpath(_guard().REDIRECTED_DATA_DIR), \
            f"{name}.WORKSPACE_DB（{db}）没跟着 DATA_DIR 走——它自己拼了路径"

    assert checked, "一个 memory_workspace 模块都没检到——护栏在对着空气生效"


# ═══════════════ ④ 逃生门是唯一不隔离的路（负向对照） ═══════════════
def test_escape_hatch_is_the_only_unisolated_path():
    """负向对照：不打开逃生门必须隔离，打开了才不隔离。

    只断言"隔离生效"是不够的——一个永远返回临时目录、根本不看环境的实现
    也能过。这里把两个方向都跑一遍：护栏必须**能**被关掉，且**只**能被那一个
    显式开关关掉。
    """
    isolated = {}
    got, created = _guard()._redirect(isolated)
    try:
        assert got, "未打开逃生门时必须隔离"
        assert created, "新建的目录必须认领所有权，否则没人负责删它"
        assert isolated["AIDUMEM_DATA_DIR"] == got
        assert os.path.isdir(got), "隔离目录必须真的建出来，不能只给个字符串"
    finally:
        if got:
            shutil.rmtree(got, ignore_errors=True)

    opened = {_guard().ESCAPE_HATCH: "1"}
    assert _redirect_into(opened) is None, "逃生门打开后不该再隔离"
    assert opened == {_guard().ESCAPE_HATCH: "1"}, \
        "逃生门打开时不许改动环境（数据目录、日志目录都不许动）——" \
        "这条路要把真目录原样交出去"


def test_preexisting_data_dir_is_overridden_not_respected():
    """已有的 AIDUMEM_DATA_DIR 必须被**覆盖**，不是"尊重"。

    生产配置本身就指着生产数据。"尊重已有设置"听起来礼貌，实际等于
    在最危险的那一种环境里把护栏关掉——那正是这次踩坑的现场。
    """
    env = {"AIDUMEM_DATA_DIR": "/var/lib/aidumei/prod-data"}
    got = _redirect_into(env)
    assert got and env["AIDUMEM_DATA_DIR"] == got, \
        "已有的 DATA_DIR 没被覆盖——部署环境里跑测试仍会写生产库"
    assert env["AIDUMEM_DATA_DIR"] != "/var/lib/aidumei/prod-data"


# ═══════════════ ⑤ 逃生门不许变成默认 ═══════════════
def test_escape_hatch_requires_the_exact_opt_in_value():
    """逃生门只认 `"1"`。含糊的值一律按"没开"处理。

    像 `"0"` / `"false"` / `""` 这种，人写的时候意思是"别开"，
    要是被当成"开了"，护栏就会在一句手滑里静默失效。
    """
    for value in ("0", "false", "", "true", "yes", "on", " 1", "1 "):
        env = {_guard().ESCAPE_HATCH: value}
        assert _redirect_into(env), \
            f"{_guard().ESCAPE_HATCH}={value!r} 不该被当成打开逃生门"

    assert _guard().ESCAPE_HATCH == "AIDUMEI_TEST_ALLOW_REAL_DATA_DIR", \
        "逃生门改名了——名字就是它的全部威慑力，改名必须同步改文档与 CHANGELOG"


# ═══════════════ ⑥ 清理只删自己建的那一个 ═══════════════
def _child_conftest(env_extra: dict) -> tuple[str, bool]:
    """在子进程里 import 一次根 conftest，回报它选的目录与所有权。

    子进程是真的跑一遍、真的退出、atexit 真的执行完——这条对照的全部价值就在
    "真退出"三个字上：所有权判断错了，磁盘上立刻少一个目录，断言当场就能抓到。
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("AIDUMEM_DATA_DIR", "AIDUMEM_LOG_DIR",
                        _guard().ESCAPE_HATCH)}
    env.update(env_extra)
    out = subprocess.run(
        [sys.executable, "-c",
         "import conftest;print(conftest.REDIRECTED_DATA_DIR or '');"
         "print(int(bool(conftest.OWNS_REDIRECTED_DIR)))"],
        cwd=_REPO, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, f"子进程 import conftest 失败：{out.stderr[-400:]}"
    lines = out.stdout.strip().splitlines()
    return lines[0], lines[1] == "1"


def test_child_process_must_not_delete_the_inherited_data_dir():
    """继承来的隔离目录，子进程退出时**不许**删——这条是一次真实事故的看门人。

    套件里有用例会另起 pytest 子进程（README 用例数护栏就要 `--collect-only`
    数一遍真实用例数）。子进程继承 env、沿用父进程的目录，若清理不分主客，
    它退出时就把父进程正在用的数据目录整棵删了。

    那次报出来的是 `ducky/wal_engine.py` 的 `FileNotFoundError: .../wal/...`，
    看着像"产品在新克隆上建不出 WAL 目录"——一个毫不相干的缺陷。护栏的 bug
    会伪装成产品的 bug，所以这条必须端到端跑一个真子进程来钉。
    """
    inherited = tempfile.mkdtemp(prefix=_guard().DIR_PREFIX)
    try:
        chosen, owns = _child_conftest({"AIDUMEM_DATA_DIR": inherited})
        assert os.path.realpath(chosen) == os.path.realpath(inherited), \
            f"子进程没沿用继承来的目录（{chosen}）——父子两边会写到两处去"
        assert owns is False, "沿用别人的目录却认领了所有权——清理时就会越权删"
        assert os.path.isdir(inherited), (
            "子进程退出后把父进程的数据目录删了——套件后半程所有落盘用例都会崩，"
            "且报错会指向产品代码，查半天查不到这里"
        )
    finally:
        shutil.rmtree(inherited, ignore_errors=True)


def test_owner_process_still_cleans_up_its_own_data_dir():
    """正向对照：自己建的目录，退出时必须删干净。

    只钉"不许删别人的"是不够的——一个从不清理的实现也能过上一条。
    临时目录攒在验收机上没人删，是另一种形式的不干净。
    """
    chosen, owns = _child_conftest({})
    assert chosen and owns is True, "自己新建的目录必须认领所有权"
    assert os.path.basename(chosen).startswith(_guard().DIR_PREFIX)
    assert not os.path.exists(chosen), \
        f"子进程退出后自己建的临时目录还在（{chosen}）——atexit 清理没生效"
