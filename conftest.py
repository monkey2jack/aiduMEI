"""conftest.py — 全套用例的数据目录隔离（v20.0）

为什么这个文件必须存在
──────────────────────
``ducky/utils.py`` 里 ``DATA_DIR`` 在 **import 那一刻**就定型了：

    DATA_DIR = os.environ.get("AIDUMEM_DATA_DIR") or os.path.join(BASE_DIR, "data")

于是「在一棵已经部署好的树里跑一遍 pytest」这件看着完全无害的事，会让
一部分用例（走真路由、真落盘的那些，例如 workspace 持久化）把测试行写进
**那棵树的生产库**。

这不是推演，是 v20.0 验收当天真实发生的事：在生产树里跑全量套件之后，
``data/workspace.db`` 里多出了三条 ``alice`` 的测试行，``data/qdrant/``
下多出了一个没有任何进程持有的 ``.lock``。用例文件自己还写着
「绝不碰仓库 data/」——它说的是它自己那几条，管不住别人。而受害者恰恰是
最该被保护的人：把包装好、原地跑一遍测试确认能用的**用户**。

所以这里在**任何 ducky 模块被 import 之前**，把 ``AIDUMEM_DATA_DIR``
指到本次会话专用的临时目录。三条口径：

1. **无条件生效**。不看环境里原来指向哪儿——生产配置本身就指着生产数据，
   "尊重已有设置"等于把这个坑原样留着。
2. **有且只有一个响亮的逃生门**：``AIDUMEI_TEST_ALLOW_REAL_DATA_DIR=1``。
   要让用例写真目录，必须显式说出口；不留任何沉默的通路。
3. **只删自己建的那一个**。套件里有用例会另起 pytest 子进程，子进程继承 env、
   沿用同一个目录；谁建的谁删，否则子进程退出时会把父进程正在用的目录删掉
   （这事真发生过，详见 ``_redirect`` 的注释）。
4. **谁改坏的谁红**。用例跑完这两个环境变量必须还在原处；被 ``pop`` 掉的
   （"删掉"不是"还原"）当场红在肇事的那条上，而不是红在四百条之后的护栏上。
5. **有测试盯着**（``tests/test_v20_test_data_isolation.py``）：护栏被摘掉、
   逃生门变成默认、清理越权删了别人的目录、或对账闸失灵，用例先红。

conftest.py 放在仓库根，是因为 pytest 会在收集任何测试模块**之前**导入它——
这个「之前」是整套隔离的全部立足点，换个位置就不成立了。
"""
from __future__ import annotations

import atexit
import os
import pytest
import shutil
import tempfile

#: 唯一的逃生门。名字里带 TEST，是为了让它在任何 env 列表里都显眼。
ESCAPE_HATCH = "AIDUMEI_TEST_ALLOW_REAL_DATA_DIR"

#: 隔离目录的前缀，也是"这个目录是我们自己建的"的唯一识别标志。
DIR_PREFIX = "aidumei_test_data_"

#: 被隔离用例写入的数据目录（None 表示逃生门打开、未做隔离）。
REDIRECTED_DATA_DIR: str | None = None

#: 本进程是不是这个目录的**主人**（只有主人才负责删它）。见 `_redirect` 的注释。
OWNS_REDIRECTED_DIR: bool = False


def _redirect(env) -> tuple[str | None, bool]:
    """把数据目录与日志目录改指到临时目录，返回 ``(目录, 本进程是否是它的主人)``。

    ``AIDUMEM_LOG_DIR`` 一并改：``ducky/utils.py`` 里它和 ``DATA_DIR`` 是同一句
    话的两半（都默认落在安装根下），漏掉它就等于「数据不写你的了，日志还写」。

    逃生门打开时返回 ``(None, False)`` 且**不动** env——这是唯一一条不隔离的路。
    抽成函数是为了能被负向对照直接调用：护栏的行为本身要可证伪，
    不能只靠"看上去写了"。

    为什么第二个返回值必须存在
    ──────────────────────────
    套件里有用例会**再起一个 pytest 子进程**（例如 README 用例数护栏要
    `--collect-only` 数一遍真实用例数）。子进程继承 env、走下面的"沿用"分支，
    于是父子两个进程指着同一个目录——但目录只有一个主人。第一版没分主客，
    子进程退出时 atexit 把**父进程正在用的**数据目录整棵删了，父进程后半程
    所有落盘用例随之崩掉。

    那次的现场特别值得记一笔：报出来的是 ``ducky/wal_engine.py`` 里
    ``FileNotFoundError: .../wal/mem_mutations.wal``，看着像"产品在新克隆上
    建不出 WAL 目录"这个完全不同的缺陷——`WALEngine.__init__` 明明 mkdir 过。
    真相是目录建好之后被自己的护栏删掉了。**护栏的 bug 会伪装成产品的 bug**，
    所以清理这件事必须问一句"这目录是我建的吗"，而不是"我知道它在哪"。
    """
    if env.get(ESCAPE_HATCH) == "1":
        return None, False

    # 已经是我们自己建的隔离目录，就沿用它，不另开一个。
    # 这条不是图省事：`ducky.utils.DATA_DIR` 在 import 时**冻结**，本模块若被
    # 重复执行（有人另加载一份 conftest 副本）而每次都换新目录，env 就会漂到
    # 冻结值之外——"环境变量说一处、代码写另一处"，比不隔离更难查。
    # 注意这里只认自己的前缀：外面传进来的真目录（含生产配置）一律照旧覆盖。
    existing = env.get("AIDUMEM_DATA_DIR")
    if existing and os.path.basename(existing).startswith(DIR_PREFIX) \
            and os.path.isdir(existing):
        env.setdefault("AIDUMEM_LOG_DIR", os.path.join(existing, "logs"))
        return existing, False

    path = tempfile.mkdtemp(prefix=DIR_PREFIX)
    env["AIDUMEM_DATA_DIR"] = path
    env["AIDUMEM_LOG_DIR"] = os.path.join(path, "logs")
    return path, True


REDIRECTED_DATA_DIR, OWNS_REDIRECTED_DIR = _redirect(os.environ)

if REDIRECTED_DATA_DIR and OWNS_REDIRECTED_DIR:
    # 只删自己建的那一个：沿用别人（父进程）目录的进程一律不许碰。
    # 删不掉（还有连接没关）也不许让退出码变红——清理失败是清理的问题，
    # 不是被测代码的问题。
    atexit.register(shutil.rmtree, REDIRECTED_DATA_DIR, True)


if REDIRECTED_DATA_DIR:
    import pytest

    #: 隔离就靠这两个变量。基线取 `_redirect` 之后**环境里实际的样子**，
    #: 不是照公式重算一遍——沿用分支下 `AIDUMEM_LOG_DIR` 可能是继承来的，
    #: 重算出来的值会和实际不符，于是每条用例都红。
    _ISOLATION_ENV = {k: os.environ[k]
                      for k in ("AIDUMEM_DATA_DIR", "AIDUMEM_LOG_DIR")
                      if k in os.environ}

    @pytest.fixture(autouse=True)
    def _isolation_env_must_survive_each_test():
        """每条用例跑完对一遍隔离环境变量：**谁改坏的谁红**。

        为什么不是"末尾统一查一次"：v20.0 验收当天，
        ``tests/test_hermes_plugin.py`` 里一句 ``os.environ.pop("AIDUMEM_DATA_DIR")``
        （本意是收尾，实际是"删掉"而非"还原"）把护栏设的值抹没了，红的却是
        四百条之后的隔离护栏用例——报错指着无辜的人，真正的肇事者一路绿灯。
        **坏掉的样子要长在坏掉的地方。**

        而且那次只在**用户那台机器**上现形：肇事文件在没装宿主的开发机上整份
        skip，本地全绿、生产一红。所以这道闸放在 conftest 里对全套生效，
        而不是指名盯住某个文件。

        先还原再断言：还原是不让后面的用例连坐（一个泄漏不该炸出一串假红），
        断言是不让肇事者混过去。两件事都要做，顺序不能颠倒。
        """
        yield
        drifted = {k: os.environ.get(k) for k, v in _ISOLATION_ENV.items()
                   if os.environ.get(k) != v}
        os.environ.update(_ISOLATION_ENV)
        assert not drifted, (
            f"这条用例跑完把隔离环境变量改坏了：{drifted}"
            "（已自动还原，后面的用例不受影响）。收尾请用 monkeypatch 或 "
            "unittest.mock.patch.dict——pop 是「删掉」不是「还原」，"
            "本来有值的变量会被抹成没有"
        )

# ── 测试临时目录回收（v20.2.4 · 生产机实测触发）────────────────────────
#
# 2026-08-28 在生产机上清理战场时数出来：`/tmp` 下有 **3573 个** `aidumem_*`
# 临时目录、146MB，从 08-24 一路攒到那天。根因是 tests/ 里 46 处
# `tempfile.mkdtemp()` 绝大多数不注册清理 —— 而 pytest 自己的 `tmp_path`
# 本来就带「保留最近 3 次、更老的自动删」，`mkdtemp` 绕过了那套机制。
#
# 删一次不算修好，下周照样攒回来。所以做**会话级兜底**，而不是逐个去改 46 处：
#   ① 删本次会话新出现的（精确到本次，不碰别人的）；
#   ② 顺带收掉 3 天以上的历史积累。
#
# 三道安全约束（这段代码在删目录，写清楚为什么它不会删错）：
#   · 只认 `aidumem_` 这一个前缀 —— 实测 45 个 mkdtemp 位点全用它；
#   · 只在系统临时目录内动手，且用 realpath 复核，防符号链接逃逸；
#   · 任何失败都静默 —— 清理失败绝不该把测试结果染红。
_TMP_PREFIX = "aidumem_"
_STALE_DAYS = 3


def _reapable(root: str):
    import glob
    import os as _o
    real_root = _o.path.realpath(root)
    out = []
    for d in glob.glob(_o.path.join(root, _TMP_PREFIX + "*")):
        rp = _o.path.realpath(d)
        if _o.path.isdir(rp) and rp.startswith(real_root + _o.sep):
            out.append(rp)
    return set(out)


_TMP_BEFORE: set = set()


def pytest_configure(config):
    """在 **collection 之前**拍下 before 快照。

    时序是这条清理的命门：不少测试在**模块顶层**调 `mkdtemp`（import 期，
    也就是 collection 期）。而 session fixture 的 setup 发生在 collection
    **之后** —— 它拿到的 before 已经把那些目录算进「别人的」，于是永远不删。
    实测就是这么发现的：跑完一轮，`/tmp` 里仍稳定剩 1 个目录。
    """
    global _TMP_BEFORE
    try:
        _TMP_BEFORE = _reapable(tempfile.gettempdir())
    except Exception:
        _TMP_BEFORE = set()


@pytest.fixture(scope="session", autouse=True)
def _reap_test_tempdirs():
    """会话结束时回收本仓测试留下的临时目录。见上方成因注释。"""
    import time      # 模块级没有 time；shutil/tempfile 用模块级的（禁重复 import 遮蔽）

    root = tempfile.gettempdir()
    before = _TMP_BEFORE

    yield

    try:
        now = time.time()
        after = _reapable(root)
        doomed = set(after) - set(before)          # ① 本次会话新增的
        for d in after:                             # ② 3 天以上的历史积累
            try:
                if now - os.path.getmtime(d) > _STALE_DAYS * 86400:
                    doomed.add(d)
            except OSError:
                pass
        for d in doomed:
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass        # 清理失败不许影响测试结论


# ── 会话收尾（v20.3.2 正式版）────────────────────────────────────────────────
# 现象：全量 1714 passed 之后，解释器退出期偶发
#   libc++abi: terminating due to uncaught exception of type std::__1::system_error:
#   recursive_mutex lock failed: Invalid argument   （Abort trap: 6，今日 9 跑 2 崩）
# 现场：会话末仍活着的线程只有 mem0 遥测的 posthog Consumer 与 aiduMEM-coalesce-flush；
# 崩点在 C++ 静态析构（grpcio / onnxruntime 随 qdrant_client / fastembed 被 import），
# 是第三方库在 macOS 上有名的终结期竞态，与被测代码无关 —— 但它让 push_gate 的
# 测试关把「全绿」读成「红」。处置分两步：先礼后兵 —— 叫停全部后台循环并 join，
# 显式跑完本文件登记的清理；然后带着 pytest 自己的退出码硬退出，不进入原生析构。
# 只在测试进程生效，生产服务是长驻进程不经过这条路。设 AIDUMEI_TEST_HARD_EXIT=0 可关闭。
_SESSION_EXIT_STATUS = {"code": None}


def pytest_sessionfinish(session, exitstatus):
    _SESSION_EXIT_STATUS["code"] = int(exitstatus)
    try:
        from ducky.shutdown import request_shutdown
        request_shutdown()
    except Exception:
        pass
    import threading as _th
    import time as _t
    deadline = _t.monotonic() + 3.0
    for th in list(_th.enumerate()):
        if th is _th.main_thread() or not th.name.startswith("aiduMEM-"):
            continue
        th.join(max(0.0, deadline - _t.monotonic()))
    try:  # mem0 遥测消费者线程：让它把队列刷完并退出
        import posthog as _ph
        _ph.shutdown()
    except Exception:
        pass


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    if os.environ.get("AIDUMEI_TEST_HARD_EXIT", "1") == "0":
        return
    code = _SESSION_EXIT_STATUS["code"]
    if code is None:
        return
    import logging as _lg
    import sys as _sys
    # 只删自己建的那一个（与上方 atexit 同一句判据）：套件里有用例会再起 pytest 子进程，
    # 子进程沿用父进程目录 —— 第一版这里无条件 rmtree，把父进程后半程 29 条落盘用例
    # 全部打成 FileNotFoundError（现场与本文件开头那段记录一字不差：护栏的 bug 伪装成产品的 bug）。
    if REDIRECTED_DATA_DIR and OWNS_REDIRECTED_DIR:
        try:
            shutil.rmtree(REDIRECTED_DATA_DIR, True)
        except Exception:
            pass
    _lg.shutdown()
    _sys.stdout.flush()
    _sys.stderr.flush()
    os._exit(code)
