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
4. **有测试盯着**（``tests/test_v20_test_data_isolation.py``）：护栏被摘掉、
   逃生门变成默认、或清理越权删了别人的目录，用例先红。

conftest.py 放在仓库根，是因为 pytest 会在收集任何测试模块**之前**导入它——
这个「之前」是整套隔离的全部立足点，换个位置就不成立了。
"""
from __future__ import annotations

import atexit
import os
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
