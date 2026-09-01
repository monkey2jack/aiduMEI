"""v20.0 审计后修复的负向对照（甲类）。

这一批用例的来路和别的不一样：它们不是从需求长出来的，是从两份外部审计
（用户视角 + 高等级代码审计）逐条落到代码上之后长出来的。每条用例点名一条
甲类缺陷，并且**必须在修复前是红的** —— 只断言"修好之后是对的"的用例，
证明不了自己有射程。

命名规则：``test_jia<N>_<一句话说清它守什么>``。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import time

import pytest

import ducky.utils as utils


_TMP = tempfile.mkdtemp(prefix="aidumem_v20_audit_fix_")
_DB = os.path.join(_TMP, "facts.db")
_TEXT_DB = os.path.join(_TMP, "text_fts.db")
utils.FACTS_DB = _DB
utils.TEXT_FTS_DB = _TEXT_DB


@pytest.fixture(autouse=True)
def _fresh_db():
    """每条用例一张干净的 core_memory 表；建表交给产品代码自己做。"""
    utils.FACTS_DB = _DB
    utils.TEXT_FTS_DB = _TEXT_DB

    import ducky.core_memory as cm

    conn = sqlite3.connect(_DB)
    for table in ("core_memory", "memory_banks"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    conn.close()

    cm._initialized = False
    cm._initialized_scopes.clear()
    yield


# ── 甲1a：一次内容更新永远不该把这一行搬到别的域 ──────────────────────


def test_jia1a_put_block_never_rewrites_row_ownership():
    """默认域里两个租户写同一个 ``block_key``，**谁也别碰谁那一行**。

    ⚠️ 这条守的是一次静默的数据搬迁，不是一次报错。

    背景：默认域对任何租户都保留 v19 裸键（这是硬契约，见
    ``test_v20_bank_scope_core.py::
    test_default_bank_keeps_the_v19_key_shape_for_every_tenant`` —— 改它会让
    升级前的存量行整体失踪）。

    v19 为这份兼容付的代价是单列主键 ``block_key`` 比
    ``(user_id, bank_id, block_key_raw)`` 粗一档：默认域里两个不同租户写同一个
    ``block_key`` 会撞进同一行。而旧代码的 upsert 还写着
    ``user_id=excluded.user_id, bank_id=excluded.bank_id`` —— 于是后写的那一方
    不只改了内容，**还把这一行的归属划到了自己名下**。没有报错、没有日志、
    ``status`` 照样是 ``ok``；从此原主人在自己的域里再也查不到这一块，而它明明
    还在库里躺着。

    ⚠️ **本条用例的断言在 v20.0pre 被改过一次，理由记在这里。** 上一版只做了
    「不可能变坏」的半步：删掉那两句归属回写，撞键时留一条 warning 并把真实归属
    回给调用方；于是它断言的是「只有一行、归属仍是 alice、返回值报 alice」。
    甲1b 把主键换成三元组之后，「撞进同一行」这个**前提本身不存在了** —— 每个
    ``(租户, bank)`` 各自一行，裸键的形状一个字节没变。断言只能跟着病情走：不再
    断言「撞了但没被搬走」，改成断言「根本撞不上」。射程没有变短 —— 归属被搬走
    的那一刻，②③ 一定红。

    判据四条：① 两个租户各自一行；② alice 的内容**没被 bob 盖掉**（这才是这条
    缺陷真正的伤害）；③ 各读各的；④ alice 自己更新自己的块照旧生效。
    """
    from ducky.core_memory import get_block, init_core_memory, put_block

    init_core_memory(user_id="alice", bank_id="default")

    first = put_block(
        "core_current_project",
        "alice 在默认域里建的这一块，归属只能是 alice",
        "alice",
        "default",
    )
    assert first["user_id"] == "alice" and first["bank_id"] == "default"

    # bob 在同一个默认域里写同一个 block_key —— 裸键，v19 下必然撞进 alice 那一行。
    second = put_block(
        "core_current_project",
        "bob 后写的内容，绝不许落到 alice 那一行上",
        "bob",
        "default",
    )
    assert second["user_id"] == "bob" and second["bank_id"] == "default"

    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    rows = {
        r["user_id"]: r["content"]
        for r in conn.execute(
            "SELECT user_id, bank_id, content FROM core_memory WHERE block_key_raw=?",
            ("core_current_project",),
        ).fetchall()
    }
    conn.close()

    # ① 两个租户各自一行 —— 三元组主键之后「撞进同一行」不再可能。
    assert set(rows) == {"alice", "bob"}, (
        f"默认域裸键下两个租户应各占一行，实得 {sorted(rows)} —— "
        "少了谁就意味着谁的那一行被另一个人吃掉了"
    )

    # ② 核心断言：alice 的内容没被 bob 盖掉。
    assert rows["alice"].startswith("alice 在默认域里建的"), (
        f"alice 那一行现在是 {rows['alice']!r} —— bob 的写入落到了 alice 的行上"
    )
    assert rows["bob"].startswith("bob 后写的内容")

    # ③ 各读各的。
    assert get_block("core_current_project", "alice", "default")["content"].startswith(
        "alice 在默认域里建的"
    )
    assert get_block("core_current_project", "bob", "default")["content"].startswith(
        "bob 后写的内容"
    )

    # ④ 正常路径不许被这道守卫打断：alice 自己更新自己的块，照旧生效。
    again = put_block(
        "core_current_project", "alice 自己更新自己的块，必须照旧生效", "alice", "default"
    )
    assert again["user_id"] == "alice"
    assert get_block("core_current_project", "alice", "default")["content"].startswith(
        "alice 自己更新"
    )


def test_jia1a_named_banks_stay_isolated_after_the_fix():
    """删掉归属回写之后，具名域的隔离**不许退化**（回归护栏）。

    甲1a 只删了 upsert 里的两句归属回写。具名域本来靠带前缀的键天生隔离，
    这条盯着它没被顺手改坏 —— 同一个 ``block_key`` 在两个具名域里必须是
    两行、两份内容，互不污染。
    """
    from ducky.core_memory import get_block, init_core_memory, put_block

    for bank in ("work", "home"):
        init_core_memory(user_id="alice", bank_id=bank)

    put_block("core_current_project", "工作域内容仅属于 work bank", "alice", "work")
    put_block("core_current_project", "家庭域内容仅属于 home bank", "alice", "home")

    work = get_block("core_current_project", "alice", "work")
    home = get_block("core_current_project", "alice", "home")

    assert work["content"] == "工作域内容仅属于 work bank"
    assert home["content"] == "家庭域内容仅属于 home bank"
    assert work["content"] != home["content"], "两个具名域撞成了同一行"


# ── 甲16：播种被 INSERT OR IGNORE 静默拒绝，CoreMemory 整块失效 ─────────


def test_jia16_renamed_default_identity_reads_its_legacy_blocks(caplog, monkeypatch):
    """改过名的默认身份，必须读得到自己那三块**存量真实内容**。

    ⚠️ 这条复现的是 v20.0 部署当天生产机器上真实发生的事，一字不差。

    链条（生产实测：``core_memory`` 3 行全部 ``user=default/bank=default``、
    全部裸键，运行时身份名下 **0** 行）：

    1. v19 存量行被增量迁移 ``ALTER TABLE ... ADD COLUMN user_id TEXT NOT NULL
       DEFAULT 'default'`` 打上 ``user_id='default'`` 的戳；
    2. 部署方把 ``AIDUMEM_DEFAULT_USER_ID`` 改成一个**非 default** 的名字，于是
       运行时的默认身份不再是 ``'default'``；
    3. 默认库对任何租户都用 v19 裸键（硬契约），播种时算出的键**正是** default
       那三行占着的裸键；
    4. ``INSERT OR IGNORE`` 撞上单列主键冲突 —— 不报错、不写入、不吭声；
    5. ``get_all_blocks(运行时身份, 'default')`` → 0 行；
    6. ``inject_context()`` → ``""``，而 ``/health`` 照样报 ``ok``。

    **CoreMemory 对这个域整块失效，日志上一个字都没有。**「静默拒绝」比「静默
    覆盖」更难发现：覆盖至少留下一行被改坏的数据，拒绝什么都不留下，调用方拿到
    的是「初始化完成」。

    ⚠️ **本条用例的断言在 v20.0pre 被改过一次，理由记在这里。** 上一版**故意
    断言这个错误行为**（「一块也读不到、注入为空、但日志里得有痕迹」），因为当时
    判定存量行归并要等 丙9 数据对账、键形状改造要等 甲1b。它的 docstring 当时就
    写明「甲1b/丙9 已落地？那这条用例的前提就变了，要连同 §1.3 一起重判」——
    现在正是那一刻，所以整条重判为「必须读得到」。

    修法是两件事合起来：甲1b 把主键换成 ``(user_id, bank_id, block_key_raw)``
    三元组；读侧靠 ``bank_contract.visible_user_ids`` 放宽 —— 改过名的默认身份
    同时看得见 ``'default'`` 名下的存量行。**一行数据都没有迁移、改写、删除**，
    所以 丙9 那套「归属靠先到先得猜一个」的数据对账不必做。

    ⚠️ 这条用例**必须** monkeypatch ``bank_contract.DEFAULT_USER_ID``：放宽只在
    ``user_id == DEFAULT_USER_ID`` 时发生（这是刻意的 —— 放宽给任意名字就等于把
    所有租户串成一个，见下面那条负向对照），而测试进程里它是 ``'default'``。随便
    挑个名字来调**不会**放宽，那样这条用例会「证明」修复无效。本仓已有四处同样
    的手法，见 ``test_v20_salience_bank_scope.py`` /
    ``test_v20_ledger_evolve_bank_scope.py`` / ``test_v20_feedback_bank_scope.py``。
    """
    import ducky.bank_contract as bank_contract
    from ducky.core_memory import (
        get_all_blocks,
        init_core_memory,
        inject_context,
        put_block,
    )

    # 第一步：造出 v19 存量 —— 三行裸键挂在 'default' 名下，内容是**真的**。
    init_core_memory(user_id="default", bank_id="default")
    real = {
        "core_user_profile": "存量真实内容：称呼、时区、沟通风格都在这里",
        "core_current_project": "存量真实内容：当前项目的目标与下一步",
        "core_key_decisions": "存量真实内容：架构选择与操作红线",
    }
    for key, content in real.items():
        put_block(key, content, "default", "default")

    # 第二步：部署方改名。放宽的开关就在这个常量上。
    monkeypatch.setattr(bank_contract, "DEFAULT_USER_ID", "dudu")

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="aiduMEM.CoreMemory"):
        init_core_memory(user_id="dudu", bank_id="default")

    # ① 三块全部读得到，而且是**存量真实内容**，不是新播的「（尚未填写）」。
    blocks = get_all_blocks(user_id="dudu", bank_id="default")
    assert set(blocks) == set(real), (
        f"改过名的默认身份只读到 {sorted(blocks)} —— 存量行对它失明了（甲16）"
    )
    for key, content in real.items():
        assert blocks[key]["content"] == content, (
            f"{key} 读出来是 {blocks[key]['content']!r} —— "
            "存量真实内容被新播的占位符盖掉了（甲1b 反噬）"
        )

    # ② 用户能感知到的症状必须消失：注入的上下文里有真东西。
    ctx = inject_context(user_id="dudu", bank_id="default")
    assert ctx, "inject_context 仍然是空的 —— 用户侧看不到任何核心记忆"
    for content in real.values():
        assert content in ctx, f"注入上下文里少了这一块：{content!r}"

    # ③ 一行数据都不许动：库里仍然只有那三行，仍然挂在 'default' 名下。
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT user_id, block_key_raw FROM core_memory").fetchall()
    conn.close()
    assert {r["user_id"] for r in rows} == {"default"}, (
        f"存量行的归属被动过了：{sorted({r['user_id'] for r in rows})} —— "
        "读侧放宽绝不允许伴随任何写入（迁移、改写、搬家都不行）"
    )
    assert len(rows) == len(real), (
        f"库里变成了 {len(rows)} 行 —— 播种给自己插了新的占位符，"
        "它们会按「精确归属优先」排在存量真实内容前面（甲1b 反噬）"
    )

    # ④ 修好之后这条路径是干净的，不许再有 WARNING（假红灯一样害人）。
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"这已经是一条修好的路径，却仍在报警：{warnings!r}"

    # ⑤ 初始化日志必须说真话：三块都读得到，就报 3/3。
    init_lines = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and "初始化完成" in r.getMessage()
    ]
    assert init_lines and "3/3" in init_lines[-1], (
        f"三块都读得到，日志却报 {init_lines!r} —— 日志本身在撒谎，比没有日志更糟"
    )


def test_jia16_seed_stays_quiet_when_nothing_is_blocked(tmp_path, monkeypatch, caplog):
    """正常播种**不许**报警（假红灯护栏）。

    一次假红会训练人忽略红灯 —— 所以 甲16 这道警告必须证明自己只在真出事时响。
    这里跑两条干净路径：全新的默认域、全新的具名域，两条都不许出现 WARNING，
    并且初始化日志必须报满额 ``3/3``。

    v20.3.1：本条曾用共享 scope 名（"solo"），在生产沙箱形态下（多出的
    Hermes 集成轴）被前序用例先初始化 → `init_core_memory` 幂等提前
    return、不打日志 → 本条红。这是测试间状态污染，不是产品缺陷：
    幂等语义本身是对的。修法是**自证前提**——每个 scope 只属于本条，
    用 tmp 隔离库 + 专属前缀，不与全仓任何用例共享名字空间。
    """
    import os
    import ducky.core_memory as cm
    from ducky.core_memory import init_core_memory

    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "logs"))
    # 前提反证: 清初始化账本（连接由 conftest 隔离到 tmp，无需断），
    # 保证本条真的在跑「全新 scope」，命运不取决于前序用例碰过什么名字
    cm._initialized_scopes.clear()

    for bank_id in ("default", "work"):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="aiduMEM.CoreMemory"):
            init_core_memory(user_id="jia16-exclusive-scope", bank_id=bank_id)

        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert not warnings, (
            f"bank_id={bank_id} 是一条干净路径，却报了警："
            f"{warnings!r} —— 假红灯会训练人忽略红灯"
        )
        init_lines = [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.INFO and "初始化完成" in r.getMessage()
        ]
        assert init_lines and "3/3" in init_lines[-1], (
            f"干净路径应报 3/3，实得 {init_lines!r} —— "
            f"scope 已被别人初始化过（幂等提前 return），前提被污染"
        )


# ── 甲1b：主键换成 (user_id, bank_id, block_key_raw) 三元组 ───────────────────
#
# 这一节是 甲1a 和 甲16 共同的**根治**，也是 v20.0pre 唯一动了表结构的地方。
# v19 的单列主键 block_key 比「谁的、哪个 bank 的、哪一块」粗一档，两个后果：
#   · 写：默认域里两个租户写同一个裸键会撞进同一行（甲1a）；
#   · 播种：撞上冲突的 INSERT OR IGNORE 一声不吭地拒绝（甲16）。
# 换主键把这两个后果同时拿掉，而裸键的形状一个字节没变。
#
# 方案文档 §1.4-② 明确要求「不接受只用 rg 命中或源码字符串守卫代替运行时证明」，
# 所以这一节全是行为断言：PRAGMA 读真表、hand-build 一张真 v19 表跑真迁移。


def _make_v19_table(rows):
    """按 v19 的**原样**建一张 core_memory（单列主键、没有 scope 三列），塞进 rows。

    这是生产存量的真实形状。之后 ``_ensure_table`` 会用
    ``ALTER TABLE ... ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'``
    给这些行打上 ``user_id='default'`` 的戳 —— 那一句正是 甲16 的第一环，
    所以测试必须走这条真路径，不能自己 INSERT 一张「已经是 v20 形状」的表。
    """
    conn = sqlite3.connect(_DB)
    conn.execute("DROP TABLE IF EXISTS core_memory")
    conn.execute(
        """
        CREATE TABLE core_memory (
            block_key        TEXT PRIMARY KEY,
            content          TEXT NOT NULL,
            updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for key, content in rows.items():
        conn.execute(
            "INSERT INTO core_memory (block_key, content) VALUES (?, ?)", (key, content)
        )
    conn.commit()
    conn.close()


def _pk_columns():
    """当前 core_memory 的主键列集合（运行时读真表，不看源码）。"""
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(core_memory)") if r["pk"]}
    conn.close()
    return cols


def test_jia1b_primary_key_is_the_scope_triple():
    """建完表，主键必须是三元组 —— 用 PRAGMA 问数据库，不问源码。"""
    from ducky.core_memory import init_core_memory

    init_core_memory(user_id="alice", bank_id="default")
    assert _pk_columns() == {"user_id", "bank_id", "block_key_raw"}, (
        f"主键实为 {sorted(_pk_columns())} —— 还是 v19 的单列主键，"
        "甲1a 的静默覆盖和 甲16 的静默拒绝都还在"
    )


def test_jia1b_v19_shaped_table_migrates_without_losing_a_row(monkeypatch):
    """v19 存量表 → 重建主键 → **一行不丢、一字不改、归属不动**。

    这条是升级路径的总闸。表重建（建新表→拷数据→删旧表→改名）是这轮唯一的
    破坏性操作，它出问题的方式是「悄悄少几行」——所以这里把三件事一起钉住：
    行数、内容、归属。

    另外顺手钉住 甲4 的教训：重建刻意**没有**放进
    ``schema_bootstrap.apply_migrations``（那个函数整体裹在
    ``except Exception: logger.error("...服务继续启动...")`` 里，一次失败的表重建
    会被降级成一行日志、服务照常起来、记忆照常空），而是放在
    ``core_memory._ensure_table`` 里、失败就往上抛。
    """
    import ducky.bank_contract as bank_contract
    from ducky.core_memory import get_all_blocks, init_core_memory

    legacy = {
        "core_user_profile": "v19 存量：用户画像的真实内容",
        "core_current_project": "v19 存量：当前项目的真实内容",
        "core_key_decisions": "v19 存量：关键决策的真实内容",
    }
    _make_v19_table(legacy)
    assert _pk_columns() == {"block_key"}, "前置条件没造对：这应该是一张 v19 表"

    monkeypatch.setattr(bank_contract, "DEFAULT_USER_ID", "dudu")
    init_core_memory(user_id="dudu", bank_id="default")

    # ① 主键真的重建了。
    assert _pk_columns() == {"user_id", "bank_id", "block_key_raw"}

    # ② 一行不丢、一字不改、归属还是迁移时打的那个戳。
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT user_id, bank_id, block_key, block_key_raw, content FROM core_memory"
    ).fetchall()
    conn.close()
    assert len(rows) == len(legacy), (
        f"迁移前 {len(legacy)} 行，迁移后 {len(rows)} 行 —— 表重建吃掉了数据"
    )
    for r in rows:
        assert r["user_id"] == "default", f"存量行的归属被改成了 {r['user_id']!r}"
        assert r["bank_id"] == "default"
        assert r["block_key_raw"] == r["block_key"], "裸键的形状被动过了"
        assert r["content"] == legacy[r["block_key"]], "内容被改写了"

    # ③ 用户侧的效果：改过名的默认身份读得到这三块真内容。
    blocks = get_all_blocks(user_id="dudu", bank_id="default")
    assert {k: v["content"] for k, v in blocks.items()} == legacy


def test_jia1b_seed_still_fills_the_blocks_that_are_genuinely_missing(monkeypatch):
    """播种守卫必须**精确**：挡住已能读到的，照旧补上真缺的。

    守卫（``_seed_defaults`` 里 ``if key in visible: continue``）是为了防甲1b 的
    反噬 —— 主键换成三元组之后 ``INSERT OR IGNORE`` 不再被拒，播种会给改过名的
    默认身份插三条崭新的「（尚未填写）」，而它们按「精确归属优先」排在存量真实
    内容**前面**，等于修完 甲16 又亲手把内容盖掉一次。

    但「一刀切不播种」同样是错的：真缺的块必须补上，否则新用户拿到一个空壳。
    这条用例造一个**半缺**的存量（只有一块），要求：那一块读到存量真内容，另外
    两块正常播种。
    """
    import ducky.bank_contract as bank_contract
    from ducky.core_memory import DEFAULT_BLOCKS, get_all_blocks, init_core_memory

    _make_v19_table({"core_user_profile": "v19 存量：只有这一块留了下来"})
    monkeypatch.setattr(bank_contract, "DEFAULT_USER_ID", "dudu")
    init_core_memory(user_id="dudu", bank_id="default")

    blocks = get_all_blocks(user_id="dudu", bank_id="default")
    assert set(blocks) == set(DEFAULT_BLOCKS), (
        f"三块没凑齐：{sorted(blocks)} —— 守卫把该播的也挡了"
    )
    # 存量那一块：真内容，没被占位符盖掉。
    assert blocks["core_user_profile"]["content"] == "v19 存量：只有这一块留了下来"
    # 真缺的两块：正常播种。
    for key in ("core_current_project", "core_key_decisions"):
        assert blocks[key]["content"] == DEFAULT_BLOCKS[key]

    # 库里应当是 1 行存量（归属 default）+ 2 行新播（归属 dudu），不多不少。
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    owners = {
        r["block_key_raw"]: r["user_id"]
        for r in conn.execute("SELECT block_key_raw, user_id FROM core_memory")
    }
    conn.close()
    assert owners == {
        "core_user_profile": "default",
        "core_current_project": "dudu",
        "core_key_decisions": "dudu",
    }, f"实得 {owners} —— 存量行被搬家，或者播种落错了归属"


def test_jia1b_named_tenant_never_sees_the_legacy_placeholder_rows(monkeypatch):
    """**负向对照**：放宽只给「改过名的默认身份」，普通租户一个字也不许看见。

    这条是整节里最重要的一条。读侧放宽（``visible_user_clause``）如果放宽给任意
    ``user_id``，就等于把所有租户串成一个大池子 —— 那是比 甲16 严重得多的越权，
    而且症状是「大家都能读到东西」，看起来像修好了。

    所以：存量行挂在 ``'default'`` 名下，运行时默认身份被改名成 ``dudu``，此时
    另一个**普通**租户 ``alice`` 必须只看得到自己的播种占位符，一个存量字符串都
    不许出现在她的注入上下文里。
    """
    import ducky.bank_contract as bank_contract
    from ducky.core_memory import (
        DEFAULT_BLOCKS,
        get_all_blocks,
        init_core_memory,
        inject_context,
    )

    legacy = {
        "core_user_profile": "别人的私事：这行绝不许被 alice 看见",
        "core_current_project": "别人的项目：这行绝不许被 alice 看见",
        "core_key_decisions": "别人的决策：这行绝不许被 alice 看见",
    }
    _make_v19_table(legacy)
    monkeypatch.setattr(bank_contract, "DEFAULT_USER_ID", "dudu")

    init_core_memory(user_id="alice", bank_id="default")
    blocks = get_all_blocks(user_id="alice", bank_id="default")
    assert set(blocks) == set(DEFAULT_BLOCKS)
    for key, content in blocks.items():
        assert content["content"] == DEFAULT_BLOCKS[key], (
            f"alice 的 {key} 读出来是 {content['content']!r} —— "
            "读侧放宽漏给了普通租户，这是越权"
        )

    ctx = inject_context(user_id="alice", bank_id="default")
    for leaked in legacy.values():
        assert leaked not in ctx, f"存量内容漏进了 alice 的上下文：{leaked!r}"


def test_jia1b_explicit_write_by_renamed_default_wins_over_the_legacy_shadow(monkeypatch):
    """显式写入盖过存量影子行 —— 这是**设计如此**，把它钉住免得被当成 bug 修掉。

    三元组主键之后，改过名的默认身份显式 ``put_block``，会在自己名下**新建**一行
    （跟 ``'default'`` 那行不撞主键）；之后「精确归属优先」让新行胜出，存量行变成
    一条休眠的影子。

    这是对的：用户亲手写下的那一版就该赢，而影子行原地留着、一个字节没动 ——
    「放宽读，绝不放宽写」。写下来是因为它看起来很像「重复行」，容易被后来人当成
    脏数据顺手合并掉；合并就等于替用户扔掉一份他自己写的内容。
    """
    import ducky.bank_contract as bank_contract
    from ducky.core_memory import get_block, init_core_memory, put_block

    legacy = {
        "core_user_profile": "存量：这一块没人动过",
        "core_key_decisions": "存量：这一块马上要被显式覆盖",
    }
    _make_v19_table(legacy)
    monkeypatch.setattr(bank_contract, "DEFAULT_USER_ID", "dudu")
    init_core_memory(user_id="dudu", bank_id="default")

    put_block("core_key_decisions", "dudu 亲手写的新版本", "dudu", "default")

    # ① 显式写入胜出。
    assert (
        get_block("core_key_decisions", "dudu", "default")["content"]
        == "dudu 亲手写的新版本"
    )
    # ② 没碰过的那一块，照旧读到存量真内容。
    assert (
        get_block("core_user_profile", "dudu", "default")["content"]
        == "存量：这一块没人动过"
    )

    # ③ 影子行原地不动：那个裸键下应当是两行，存量那行内容一个字没变。
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    shadow = {
        r["user_id"]: r["content"]
        for r in conn.execute(
            "SELECT user_id, content FROM core_memory WHERE block_key_raw=?",
            ("core_key_decisions",),
        )
    }
    conn.close()
    assert shadow == {
        "default": "存量：这一块马上要被显式覆盖",
        "dudu": "dudu 亲手写的新版本",
    }, f"实得 {shadow} —— 存量影子行被改写或删除了（放宽读不许伴随任何写）"


def test_jia1b_migration_refuses_to_merge_colliding_rows():
    """两行撞进同一个三元组时，迁移必须**说清楚是哪一块、有几行**，然后拒绝动手。

    自动合并等于替用户扔掉一份他自己写的内容，所以只能拒绝。但拒绝的方式很重要：
    裸 INSERT 撞主键只会抛一句 ``UNIQUE constraint failed: ...``，不告诉你是哪一
    块、属于谁、有几行 —— 这个函数要在别人的生产库上跑，**起不来是可以接受的，
    起不来又说不清为什么不行**。所以撞键要自己先查、报文要点名。

    这里造的是一个混版本残留态：同一个裸键，一行存的是裸键、一行存的是带前缀的
    scoped 键（v19 单列主键放得过），但迁移后它们的三元组完全相同。
    """
    from ducky.core_memory import _migrate_pk_to_scope_triple

    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("DROP TABLE IF EXISTS core_memory")
    conn.execute(
        """
        CREATE TABLE core_memory (
            block_key        TEXT PRIMARY KEY,
            content          TEXT NOT NULL,
            updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_id          TEXT NOT NULL DEFAULT 'default',
            bank_id          TEXT NOT NULL DEFAULT 'default',
            block_key_raw    TEXT
        )
        """
    )
    for block_key, content in (
        ("core_user_profile", "先来的那一份"),
        ("default::core_user_profile", "后来的那一份"),
    ):
        conn.execute(
            "INSERT INTO core_memory (block_key, content, user_id, bank_id, block_key_raw) "
            "VALUES (?, ?, 'default', 'default', 'core_user_profile')",
            (block_key, content),
        )
    conn.commit()

    with pytest.raises(RuntimeError) as excinfo:
        _migrate_pk_to_scope_triple(conn)

    msg = str(excinfo.value)
    assert "core_user_profile" in msg, f"报文没点名是哪一块：{msg!r}"
    assert "2" in msg, f"报文没说有几行：{msg!r}"

    # 拒绝就是拒绝：两行都还在，一行都没动。
    kept = conn.execute("SELECT COUNT(*) FROM core_memory").fetchone()[0]
    conn.close()
    assert kept == 2, f"拒绝迁移却动了数据：只剩 {kept} 行"


# ── 缺陷#13：写入失败后没人回滚，线程本地连接被永久卡住 ──────────────
#
# 这一节故意不叫 test_jia*：#13 不在那两份审计清单里，是 v20.0pre 给甲1b 做
# 负向对照时新发现的 —— 那几轮跑测里冒出来的 ``database is locked`` 和
# 「9 errors in 46.87s」（≈ 9 × sqlite 默认 5 秒锁超时）不是测试环境抖动，
# 是产品代码自己的漏洞在冒烟。
#
# 成因要三件事凑齐，单看哪一件都不像问题：
#   ① sqlite3 默认 isolation_level 会在 DML 之前隐式 BEGIN；
#   ② 写入语句失败时事务是**开着**的，而原来没人 rollback；
#   ③ finally 里的 conn.close() 是 no-op（见 utils._ConnProxy
#      「close() 变 no-op，防止线程本地连接被意外关闭」），这条线程本地连接
#      会一直活到进程结束。
# 三件凑齐 = 一次失败的写入让这条连接永久持有写事务，之后这个库上每一个写入
# 方都要先等满锁超时、再收 database is locked。而日志里只有第一次那条报错，
# 线上表现就是「CoreMemory 从某一刻起再也写不进去了」。
#
# ⚠️ 炸点必须落在 **commit**，不能落在 execute。
#    在 execute 上就抛的话，语句根本没到 sqlite —— 锁没拿到、事务没开，
#    这样写出来的用例**把 rollback 删掉照样是绿的**，是个假绿灯。
#    必须让一条真的 DML 先拿到写锁，再让紧接着的那次 commit 失败。
#
# ⚠️ 如实说明：_ensure_table 里那两句 UPDATE 也补了 rollback（第四处），但它
#    **做不成单独的用例** —— 它唯一的开事务窗口紧接着就被
#    _migrate_pk_to_scope_triple 里那句 ``if conn.in_transaction: conn.commit()``
#    关掉了。没法单独引爆就不假造一条，宁可在这里写清楚。


class _CommitBomb:
    """线程本地连接的外壳：看到目标写语句就上膛，紧接着的那次 commit 炸掉。

    刻意只拦 ``execute`` 和 ``commit`` 两个方法，其余（rollback / in_transaction
    / cursor …）一律转发给真连接 —— 被测的正是产品代码有没有调 rollback，
    这层壳绝不能替它做这件事。``close()`` 也照 utils._ConnProxy 的样子做成
    no-op，因为「连接不会真的关掉」正是本缺陷的第三个前提。
    """

    def __init__(self, conn, arm_sql: str):
        self._conn = conn
        self._arm_sql = arm_sql
        self.armed = False
        self.fired = False

    def execute(self, sql, *args, **kwargs):
        cur = self._conn.execute(sql, *args, **kwargs)   # 真执行、真拿锁
        # 只上一次膛：炸完之后要用同一条连接验「下一次正常写入还能成功」，
        # 再上膛就会把自己的验证步骤也炸掉。
        if not self.fired and self._arm_sql in " ".join(str(sql).split()):
            self.armed = True
        return cur

    def commit(self):
        if self.armed:
            self.armed = False
            self.fired = True
            raise sqlite3.OperationalError("disk I/O error（注入：模拟提交失败）")
        return self._conn.commit()

    def close(self):
        pass            # 和 utils._ConnProxy 一致：线程本地连接不可关闭

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _install_bomb(monkeypatch, cm, arm_sql: str) -> "_CommitBomb":
    """把 cm._get_conn 换成「同一条真连接 + 一层炸弹壳」。"""
    real = cm._get_conn()
    bomb = _CommitBomb(real, arm_sql)
    monkeypatch.setattr(cm, "_get_conn", lambda: bomb)
    return bomb


def _outsider_write_probe() -> tuple:
    """另开一条连接去写同一个库 —— 这就是线上那条 database is locked 的复现。

    用真的 INSERT/DELETE，不用「UPDATE 成自己」：后者可能一页都不脏，
    未必去拿写锁，探针会变成假绿灯。
    """
    stamp = str(time.time())
    other = sqlite3.connect(_DB, timeout=1.0)
    try:
        other.execute(
            "INSERT INTO core_memory (block_key, content, updated_at, "
            "last_verified_at, user_id, bank_id, block_key_raw) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("__lock_probe__", "lock probe", stamp, stamp,
             "__probe__", "__probe__", "__lock_probe__"),
        )
        other.commit()
        other.execute("DELETE FROM core_memory WHERE user_id = '__probe__'")
        other.commit()
        return True, ""
    except sqlite3.OperationalError as exc:
        return False, str(exc)
    finally:
        other.close()


def test_bug13_put_block_failure_does_not_wedge_the_connection(monkeypatch):
    """put_block 写失败之后，这个库还能被别人写 —— 不许把连接卡在开着的事务里。"""
    import ducky.core_memory as cm

    cm.init_core_memory(user_id="dudu", bank_id="default")
    before = cm.get_block("core_key_decisions", user_id="dudu", bank_id="default")

    bomb = _install_bomb(monkeypatch, cm, "INSERT INTO core_memory (block_key")
    with pytest.raises(sqlite3.OperationalError):
        cm.put_block("core_key_decisions", "写到一半提交失败的内容", "dudu", "default")
    assert bomb.fired, "炸弹没响，这条用例什么都没验到"

    # ① 事务必须已经关掉（rollback 生效）
    assert bomb._conn.in_transaction is False, (
        "写失败后事务还开着 —— 缺 conn.rollback()，这条线程本地连接已经废了"
    )
    # ② 别人还能写（线上那条 database is locked 的直接复现）
    ok, err = _outsider_write_probe()
    assert ok, f"另一条连接写不进去了：{err}"
    # ③ 失败的那次写入没留下痕迹
    after = cm.get_block("core_key_decisions", user_id="dudu", bank_id="default")
    assert after["content"] == before["content"], "回滚没干净，脏数据留下来了"
    # ④ 下一次正常写入还能成功
    cm.put_block("core_key_decisions", "回滚之后仍然写得进去", "dudu", "default")
    again = cm.get_block("core_key_decisions", user_id="dudu", bank_id="default")
    assert again["content"] == "回滚之后仍然写得进去"


def test_bug13_verify_block_failure_does_not_wedge_the_connection(monkeypatch):
    """verify_block 只刷时间戳，一样会开事务；它失败后也不许卡住连接。"""
    import ducky.core_memory as cm

    cm.init_core_memory(user_id="dudu", bank_id="default")

    bomb = _install_bomb(monkeypatch, cm, "UPDATE core_memory SET last_verified_at = ?")
    with pytest.raises(sqlite3.OperationalError):
        cm.verify_block("core_user_profile", user_id="dudu", bank_id="default")
    assert bomb.fired, "炸弹没响，这条用例什么都没验到"

    assert bomb._conn.in_transaction is False, (
        "verify_block 失败后事务还开着 —— 缺 conn.rollback()"
    )
    ok, err = _outsider_write_probe()
    assert ok, f"另一条连接写不进去了：{err}"

    result = cm.verify_block("core_user_profile", user_id="dudu", bank_id="default")
    assert result.get("status") != "not_found"


def test_bug13_seed_failure_does_not_wedge_the_connection(monkeypatch):
    """播种（进程启动第一件事）失败时最要命：卡住了就等于整个服务写不进 CoreMemory。"""
    import ducky.core_memory as cm

    bomb = _install_bomb(monkeypatch, cm, "INSERT OR IGNORE INTO core_memory")
    with pytest.raises(sqlite3.OperationalError):
        cm.init_core_memory(user_id="dudu", bank_id="default")
    assert bomb.fired, "炸弹没响，这条用例什么都没验到"

    assert bomb._conn.in_transaction is False, (
        "播种失败后事务还开着 —— 这正是线上「CoreMemory 从此写不进去」的成因"
    )
    ok, err = _outsider_write_probe()
    assert ok, f"另一条连接写不进去了：{err}"

    # 重试必须能把三块补齐（播种本身是幂等的）
    cm.init_core_memory(user_id="dudu", bank_id="default")
    blocks = cm.get_all_blocks(user_id="dudu", bank_id="default")
    assert len(blocks) == 3, f"重试后没能补齐三块：{sorted(blocks)}"


# ── 甲17：显著性只是排序加成，缺表不该杀掉整次检索 ─────────────────────


def _reset_schema_warn_registry() -> None:
    """清掉「每进程只喊一次」的降级记账。

    用 ``getattr`` 兜是有意的，不是防御性冗余：跑负向对照要把 pre-fix 的
    ``core.py`` 换回来，那份代码里还没有 ``_schema_warned`` 这个符号。若在
    fixture 里直接点属性，三条用例会在 **setup 阶段** 就 ``AttributeError``
    —— 那是一次**假红**，红得跟缺陷毫无关系，只证明了「新符号还没加」。
    一条只会因为自己引用了新符号而变红的用例，射程是零。
    """
    import ducky.salience.core as sc

    registry = getattr(sc, "_schema_warned", None)
    if registry is not None:
        registry.clear()


@pytest.fixture
def salience_db_without_table(monkeypatch, tmp_path):
    """把 salience 库指到一个**连表都没有**的空文件，用完自动还原。

    ⚠️ 故意**不**在模块级改 ``utils.SALIENCE_DB``。全套 ``tests/`` 里有 60 处
    模块级 DB 全局赋值、横跨 35 个文件，它们在 import 期互相抢同一个全局，
    字母序在后的赢 —— 本轮就是这么撞出两条与产品毫无关系的红，而且**当时那
    两条是碰巧绿的**（赢家的临时库恰好建了 salience 表，替输家把坑填上了）。
    改一个文件名就会无端翻红。

    ``get_salience_conn()`` 是调用时才读这个全局的（``utils.py`` 里连接按路径
    缓存、``close()`` 是 no-op），所以 fixture 里 monkeypatch 完全够用，根本
    不需要在 import 期动手。这条 fixture 顺便是那个坏习惯的反例。
    """
    db = tmp_path / "salience.db"
    db.touch()  # 空文件：连得上，但一张表都没有
    monkeypatch.setattr(utils, "SALIENCE_DB", str(db))

    _reset_schema_warn_registry()  # 「每进程只喊一次」的记账，跨用例必须清
    yield db
    _reset_schema_warn_registry()


def test_jia17_batch_lookup_degrades_instead_of_raising(salience_db_without_table, caplog):
    """``salience`` 表不在时，批量查询返回空 map，**不抛异常**。

    ⚠️ 这条守的是可用性，不是数据。

    原来这里是 ``try/finally`` 而没有 ``except``，于是缺表时
    ``sqlite3.OperationalError`` 一路穿过 ``scoring.py`` → ``engine.py``，
    **整次 /search 500** —— 而它只是想给结果加一点分。真实 traceback：

        ducky/engine.py:130      in search  → score_and_rank_candidates(
        ducky/scoring.py:143     in score_and_rank_candidates → get_batch_salience_records(
        ducky/salience/core.py   in get_batch_salience_records
        E  sqlite3.OperationalError: no such table: salience

    判据一句话：富化查询失败，降级的应该是排序质量，不该是可用性。
    """
    import ducky.salience.core as sc

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="aiduMEM.salience"):
        got = sc.get_batch_salience_records(["m1", "m2"])

    assert got == {}, f"缺表时应降级成空 map，实得 {got!r}"

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "整个显著性加成都失效了，日志里却一个字都没有 —— 静默降级与正常无从区分"
    assert "甲17" in "\n".join(warnings), f"警告没标缺陷编号，运维无从查证：{warnings!r}"


def test_jia17_search_scoring_survives_a_missing_salience_table(salience_db_without_table):
    """把镜头拉到 ``/search`` 实际走的那一层：缺表时候选**一条都不许少**。

    上一条守叶子函数，这条守调用链 —— 缺陷是在 ``score_and_rank_candidates``
    里发作的，光证明叶子不抛还不够，得证明这一层真的能把结果排出来。
    """
    from ducky.scoring import score_and_rank_candidates

    candidates = [
        {"id": "m1", "memory": "项目组昨天定了 v20.0 这个版本号", "score": 0.9},
        {"id": "m2", "memory": "显著性只是排序加成", "score": 0.5},
    ]

    ranked = score_and_rank_candidates("v20.0", candidates, user_id="default", limit=10)

    assert len(ranked) == 2, (
        f"缺表把候选吃掉了，实得 {len(ranked)} 条 —— "
        "拿不到排序加成时应按「没有这一档」去排，而不是少给结果"
    )
    assert {it["id"] for it in ranked} == {"m1", "m2"}


def test_jia17_access_boost_never_kills_the_request(salience_db_without_table):
    """``/search`` 上的第二个出口：访问提权提不动，也不许把请求带走。

    ``boost_salience_for_results`` 在 ``hot/search.py`` 里被调两次 —— 一次完全
    裸调，另一次裹在 ``except ImportError`` 里，而 ``OperationalError`` 不是
    ``ImportError``，照样逃出去。这是一次纯副作用：提不动权，检索结果一个字
    都不该少。
    """
    import ducky.salience.core as sc

    sc.on_memory_accessed("m1")  # 不抛就是通过

    # 顺带把上层聚合点也走一遍，形状与 /search 里传进去的一致。
    from ducky.mem0_runtime import boost_salience_for_results

    boost_salience_for_results([{"id": "m1"}, {"memory_id": "m2"}])


def test_jia17_salience_still_works_when_the_table_is_there(monkeypatch, tmp_path, caplog):
    """表在的时候，显著性**必须照旧生效**（假绿灯护栏）。

    ⚠️ 这条是上面三条的对账单。「缺表就降级」有一种极其廉价的假修法：让读
    路径永远返回空。那样上面三条全绿，而显著性从此整体失效 —— 缺陷换了个样子
    活下来，还带着一身绿灯。所以必须有一条反向证明：表在时读得到、写得进、
    且**一声警告都不许有**（真降级了才准喊）。
    """
    import ducky.salience.core as sc
    from ducky.salience.db import _ensure_db

    db = tmp_path / "salience.db"
    monkeypatch.setattr(utils, "SALIENCE_DB", str(db))
    _reset_schema_warn_registry()
    _ensure_db()  # 建真表

    now = time.time()
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO salience (memory_id, salience, last_access, access_count, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("m1", 0.92, now, 7, now),
    )
    conn.commit()
    conn.close()

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="aiduMEM.salience"):
        got = sc.get_batch_salience_records(["m1", "m2"])

        # ① 读得到，而且值是真的 —— 不是被降级路径抹平的空 map。
        assert "m1" in got, f"表明明在，却读不到任何显著性记录：{got!r}"
        assert abs(got["m1"]["salience"] - 0.92) < 1e-9
        assert got["m1"]["access_count"] == 7
        assert "m2" not in got, "库里没有 m2，不该凭空造出一条记录"

        # ② 写得进 —— 甲17 把 on_memory_accessed 整个函数体挪进了 try，
        #    这里证明提权本身没被改坏。
        sc.on_memory_accessed("m1")
        after = sc.get_batch_salience_records(["m1"])
        assert after["m1"]["access_count"] == 8, (
            f"访问提权没生效，access_count 仍是 {after['m1']['access_count']}"
        )
        assert after["m1"]["salience"] >= got["m1"]["salience"], "提权反而把显著性调低了"

    # ③ 干净路径一声警告都不许有（假红灯会训练人忽略红灯）。
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"表在得好好的，却报了降级告警：{warnings!r}"

    _reset_schema_warn_registry()


def test_jia17_a_locked_db_is_not_mistaken_for_a_missing_table(monkeypatch):
    """库被锁 ≠ 没有加成：非「缺表/缺列」的故障必须原样抛出。

    这是 ``bank_contract.is_legacy_schema_error`` 那篇判据在本处的落地。若用
    ``except Exception`` 一把兜住，那么库被锁、磁盘写满、连接被回收，全都会命中
    同一个降级分支 —— 排序被悄悄整体抹平，**而返回值形状与一次正常查询一模一样**。
    静默失败与成功无从区分，比直接报错糟得多。
    """
    import ducky.salience.core as sc

    class _LockedConn:
        def execute(self, *_a, **_kw):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            pass

    monkeypatch.setattr(sc, "get_salience_conn", lambda: _LockedConn())

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        sc.get_batch_salience_records(["m1"])

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        sc.on_memory_accessed("m1")


# ════════════════════════════════════════════════════════════════════
# 甲7 / 甲8 —— 降级前先验明病因（8 个位点）
# ════════════════════════════════════════════════════════════════════
#
# 这八处原来都是 `except Exception` / `except sqlite3.OperationalError`
# 一把兜住就降级，而降级动作恰好是**摘掉 (user_id, bank_id) 作用域过滤**。
# 于是「老库还没迁出作用域列」（该降级）与「库被锁一下」（绝不该降级）
# 共用同一个出口——隔离能被一次瞬时故障摘掉，它就不叫隔离。
#
# 八处的后果**不是同一种形状**，用例里逐条如实标注，不许拉平夸大。后果最重的
# 是两类，方向不同，不必强行排出高下：
#   甲8-7（opinion._fact_scope）写侧完整性：把**持久化的作用域戳写错**，且
#          upsert 会顺手把一条本来盖对了戳的旧行改成 default——别处是读漏，
#          这处是账本改错；日志还是 debug 级，生产 INFO 下一个字都不出现
#   甲8-3 / 甲8-5 读侧机密性：摘掉域过滤 → scene.summary 跨库泄漏重新打开
# 其余：
#   甲8-2（federation/schema）唯一一处**返回值撒谎**：吞掉之后照样 status=ok
#   甲8-4 中等：具名库 fail-closed，但默认域调用方能改具名库事实的信任分
#   甲7 中等：蒸馏跨库合并，merge 输家会被归档（静默数据销毁）
#   甲8-1 / 甲8-6 轻：一个止于难查，一个止于多枚举几个标签
#
# 判据轴也不是同一条，这一点比用例本身更容易搞错：
#   四处看 raise / 不 raise
#   甲8-1 看**报错文案**（修前修后都 raise，修前抛的是被掩盖后的二次错误）
#   甲8-2 看**返回值**（修前 ok / 修后 error，raise 与否完全一样）
#   甲8-4 / 甲8-7 有两条轴（危害 + 静默），刻意**不用** pytest.raises 包起来：
#          那样修前会先在「没抛异常」上断掉，危害那条断言一次也跑不到，也就
#          无从证明它自己有射程。改成把两条轴的问题收进 problems 一次性报出。


class _SelectiveFailCursor:
    """只在 SQL 命中判据时抛错的游标；其余一律转发给真游标。"""

    def __init__(self, real, trips):
        self._real = real          # 必须最先赋值，否则 __getattr__ 自我递归
        self._trips = trips

    def execute(self, sql, *a, **kw):
        if self._trips(sql):
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _SelectiveFailConn:
    """包一条**真**连接，只在 SQL 命中判据时抛 OperationalError。

    刻意不用 MagicMock：假连接一旦什么都答应，就分不清「代码走了降级路径」
    和「代码根本没查库」——而这两件事在本批用例里正是要区分的东西。降级分支
    必须真的打在真 SQLite 上跑一遍，返回真行，才能证明它确实把域过滤摘了。
    """

    def __init__(self, real, trips):
        self._real = real          # 同上：先赋值，避免 __getattr__ 递归
        self._trips = trips

    def execute(self, sql, *a, **kw):
        if self._trips(sql):
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *a, **kw)

    def cursor(self, *a, **kw):
        return _SelectiveFailCursor(self._real.cursor(*a, **kw), self._trips)

    def close(self):
        # 连接由 fixture 统一收尾：被测代码的 close() 不能真关掉，否则同一条
        # 用例里后续断言拿不到库。
        pass

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def facts_db():
    """一张由**产品自己的迁移函数**建出来的 facts 表。

    绝不手写 DDL：手写的表一旦与 schema_bootstrap 漂移，用例就会开始验证一个
    产品里并不存在的形状。
    """
    import ducky.schema_bootstrap as sb

    sb._done = False
    sb.ensure_core_schema(force=True)
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("DELETE FROM facts")
    conn.commit()
    yield conn
    conn.close()


def _seed_fact(conn, category, fact_key, fact_value, user_id, bank_id):
    conn.execute(
        "INSERT INTO facts (category, fact_key, fact_value, user_id, bank_id, "
        "archived, created_at, updated_at) VALUES (?,?,?,?,?,0,?,?)",
        (category, fact_key, fact_value, user_id, bank_id,
         time.strftime("%Y-%m-%dT%H:%M:%S"), time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_jia7_autodream_refuses_to_merge_across_banks_on_a_locked_db(monkeypatch, facts_db):
    """甲7：蒸馏取事实时库被锁 —— 不许退回「全库一律 default 域」。

    降级分支自己没错（老库确实是单一域），错在它不看病因。而它的下游是
    ``_cluster_by_prefix`` → ``_simple_merge``：merge 的输家会被 ``archived=1``
    归档。一次瞬时锁库就能让甲库的事实把乙库的事实当「重复」归档掉——静默数据
    销毁，而返回值形状与一次正常蒸馏一模一样。
    """
    import ducky.autodream as ad

    _seed_fact(facts_db, "identity", "k1", "甲库的事实", "u_a", "bank_a")
    _seed_fact(facts_db, "identity", "k1", "乙库的事实", "u_b", "bank_b")

    monkeypatch.setattr(
        ad, "get_facts_conn",
        lambda: _SelectiveFailConn(facts_db, lambda sql: "bank_id" in sql),
    )

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        ad._get_recent_facts(days=7)


def test_jia8_1_core_memory_probe_failure_reports_the_real_cause(monkeypatch, facts_db):
    """甲8-1：探针失败 ≠ 缺这一列 —— 报错必须说真话。

    本处**修前修后都 raise**，所以判据不是 raise 与否，是**抛的是哪一个错**：
    上面的 ``CREATE TABLE IF NOT EXISTS core_memory`` 已经声明了
    ``last_verified_at``，所以修前吞掉探针错误之后那条 ``ALTER TABLE`` 必然
    再失败一次，抛出来的是 ``duplicate column name`` —— 真正的病因（库被锁）
    被二次错误盖住。修后抛的是那个真病因。

    这一处的危害**止于难查**，用例如实照此立断言，不夸大成越域读取。
    """
    import ducky.core_memory as cm

    monkeypatch.setattr(
        cm, "_get_conn",
        lambda: _SelectiveFailConn(
            facts_db, lambda sql: "last_verified_at FROM core_memory" in sql),
    )

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        cm._ensure_table()


def test_jia8_2_federation_migration_stops_saying_ok_when_it_failed(monkeypatch, tmp_path):
    """甲8-2：八处里唯一**返回值撒谎**的一处 —— 迁移失败不许再报 ok。

    判据是**返回值**，不是 raise：修前 ALTER 被吞掉、函数末尾照样
    ``{"status": "ok"}``，而 ``added`` 少一项本来就是「列已存在」的正常情形，
    两种截然不同的结局在返回值里长得一模一样。修后那个错误落进既有的
    ``except Exception`` → ``{"status": "error"}``（那个诚实的出口本来就在，
    只是这条路从来没走到过）。

    用**自己的**临时库：``ensure_federation_schema`` 会走到
    ``rebuild_facts_unique_index``，那里有 ``DROP INDEX`` 和 ``DELETE FROM facts``，
    跑在全文件共用的 ``_DB`` 上会污染别的用例。
    """
    import ducky.federation.schema as fed
    import ducky.schema_bootstrap as sb

    own_db = str(tmp_path / "facts_fed.db")
    monkeypatch.setattr(utils, "FACTS_DB", own_db)
    sb._done = False
    sb.ensure_core_schema(force=True)          # 真表，否则修前也会因「无此表」报 error（假绿）

    real = sqlite3.connect(own_db)
    real.row_factory = sqlite3.Row
    monkeypatch.setattr(
        fed, "get_facts_conn",
        lambda: _SelectiveFailConn(real, lambda sql: "ADD COLUMN agent_id" in sql),
    )
    # 逼它真去 ALTER：不然列已就位，那条 ALTER 根本不会执行
    monkeypatch.setattr(fed, "_existing_columns", lambda *_a, **_kw: set())
    monkeypatch.setattr(fed, "_migrated", False)

    result = fed.ensure_federation_schema(force=True)
    real.close()

    assert result["status"] == "error", (
        f"ALTER 失败却报了成功，调用方无从分辨迁移到底跑没跑：{result!r}")
    assert "locked" in result.get("detail", ""), (
        f"报了 error 但没带上真病因：{result!r}")


def test_jia8_3_extract_key_facts_keeps_the_scope_filter_on_a_locked_db(monkeypatch, facts_db):
    """甲8-3：八处里**读侧**后果最重的一处 —— 摘掉域过滤会重开 scene.summary 泄漏。

    ``_extract_key_facts`` 的下游 ``_cluster_scenes`` 把 ``fact_value`` 直接抄进
    ``scene.summary``；这个降级出口做的事就是把 ``AND user_id=? AND bank_id=?``
    去掉。库被锁一次，那段聚类注释声称已经堵住的跨库泄漏就重新打开。
    """
    import ducky.hot.legacy_helpers as lh

    _seed_fact(facts_db, "identity", "k_a", "甲库机密", "u_a", "bank_a")
    _seed_fact(facts_db, "identity", "k_b", "乙库机密", "u_b", "bank_b")

    monkeypatch.setattr(
        lh, "_get_facts_conn",
        lambda: _SelectiveFailConn(facts_db, lambda sql: "AND user_id=?" in sql),
    )

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        lh._extract_key_facts("identity", user_id="u_b", bank_id="bank_b")


def test_jia8_4_fact_feedback_never_falls_back_to_the_default_scope(monkeypatch, facts_db):
    """甲8-4：默认域调用方不许因为一次锁库就能改具名库事实的信任分。

    修前走到降级出口，``row_scope`` 被写成 ``("default","default")``：具名库
    调用方还算 fail-closed（域不符 → 404），但**默认域的调用方会匹配上**，
    于是这条其实属于 ``bank_a`` 的事实的 ``trust_score`` 就被改掉了。危害比
    ``_extract_key_facts`` 那处窄，方向一样。

    判据轴要跟着这个函数的既有形状走，不能照抄兄弟用例：本函数最外层有
    ``except Exception → HTTPException(500)``，所以修好之后 OperationalError
    不会裸奔出来，而是变成一个**带着真病因的 500**。而真正的缺陷证人是另一条
    轴——``trust_score`` 有没有被动过。修前：不报错、返回 ok、分数被改；修后：
    500 带真病因、分数分毫未动。

    刻意**不用** ``pytest.raises`` 包起来：那样修前会先在「没抛异常」上断掉，
    信任分那条断言一次也跑不到，也就无从证明它自己有射程。改成把两条轴的问题
    都收进 ``problems`` 一次性报出——修前一跑，两条同时现形。
    """
    from fastapi import HTTPException

    import ducky.hot.legacy_helpers as lh

    fact_id = _seed_fact(facts_db, "identity", "k_a", "甲库的事实", "u_a", "bank_a")
    before = facts_db.execute(
        "SELECT trust_score FROM facts WHERE id=?", (fact_id,)).fetchone()[0]

    monkeypatch.setattr(
        lh, "_get_facts_conn",
        lambda: _SelectiveFailConn(facts_db, lambda sql: "user_id" in sql),
    )

    err = None
    result = None
    try:
        result = lh._fact_feedback_impl(
            fact_id, helpful=True, user_id="default", bank_id="default")
    except HTTPException as exc:
        err = exc

    after = facts_db.execute(
        "SELECT trust_score FROM facts WHERE id=?", (fact_id,)).fetchone()[0]

    problems = []
    if after != before:
        problems.append(
            f"默认域调用方改掉了 bank_a 名下事实的信任分：{before} → {after}")
    if err is None:
        problems.append(f"锁库被当成了正常业务结果，返回 {result!r}")
    else:
        if err.status_code != 500:
            problems.append(
                f"锁库应是需要人看的故障（500），却报了 {err.status_code}")
        if "locked" not in str(err.detail):
            problems.append(f"报了错但没带上真病因：{err.detail!r}")
    assert not problems, "；".join(problems)


def test_jia8_5_scene_clustering_refuses_to_collapse_every_bank_into_one(monkeypatch, facts_db):
    """甲8-5：枚举作用域失败 —— 不许把全部 bank 塌成一个域。

    ``scopes = [("", "")]`` 对老库是对的，但它同时意味着聚类从此跨库跑，又落回
    ``scene.summary`` 那条泄漏。与甲8-6 同在一个函数里，靠**调用形状**分开：
    这里不传作用域，才会走到枚举分支。
    """
    import ducky.hot.legacy_helpers as lh

    _seed_fact(facts_db, "identity", "k_a", "甲库机密", "u_a", "bank_a")
    _seed_fact(facts_db, "identity", "k_b", "乙库机密", "u_b", "bank_b")

    monkeypatch.setattr(
        lh, "_get_facts_conn",
        lambda: _SelectiveFailConn(facts_db, lambda sql: "DISTINCT user_id" in sql),
    )

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        lh._cluster_scenes_impl(dry_run=True)


def test_jia8_6_category_enumeration_still_has_to_prove_the_cause(monkeypatch, facts_db):
    """甲8-6：八处里后果最轻的一处 —— 依然要验病因。

    如实记着它为什么轻：category 只是标签，下面 ``_extract_key_facts`` 仍然带着
    scope 去取事实，多枚举出的外库分类拿不到本域事实，``len(facts) < 2`` 就
    ``continue`` 掉了，**不构成越域读取**。但四个兄弟都验了病因，独留这一个不验
    没有道理。

    判据里带上 ``DISTINCT category``：只这样才不会连带打中 ``_extract_key_facts``
    的 ``SELECT *``——否则修前也会红，红在别处（假红）。
    """
    import ducky.hot.legacy_helpers as lh

    _seed_fact(facts_db, "identity", "k_b1", "乙库事实一", "u_b", "bank_b")
    _seed_fact(facts_db, "identity", "k_b2", "乙库事实二", "u_b", "bank_b")

    monkeypatch.setattr(
        lh, "_get_facts_conn",
        lambda: _SelectiveFailConn(
            facts_db, lambda sql: "DISTINCT category" in sql and "user_id" in sql),
    )

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        lh._cluster_scenes_impl(dry_run=True, user_id="u_b", bank_id="bank_b")


def test_jia8_7_locked_db_must_not_rewrite_a_correct_opinion_scope_stamp(monkeypatch, facts_db):
    """甲8-7：八处里**写侧**后果最重的一处 —— 锁库不许把盖对的戳改错。

    这一处与另外七处不是同一种伤害。别处是「读漏」「跨库合并」——错的是这一次
    调用的结果；这处错的是**留在库里的账**：``set_opinion`` 的 upsert 带着
    ``DO UPDATE SET user_id=excluded.user_id, bank_id=excluded.bank_id``，所以
    ``_fact_scope`` 一旦降级成 ``default|default``，倒霉的不只是新行——一条
    **本来盖对了戳**的旧信念行会被永久改写。事后审计拿着这条戳，会理直气壮地
    说「它属于 default 库」。

    所以用例必须是两拍，而不是一拍：
        第一拍 不锁库写一次 → 戳成 ("u_a", "bank_a")，证明正常路径是对的
        第二拍 锁住 _fact_scope 那一句、用**同一个 (fact_id, source)** 再写一次
               → 走 ON CONFLICT DO UPDATE，这才打到「改写已有行」这条真伤害
    只写第二拍是不够的：新行盖错戳顶多是「这条记漏了」，改写旧行才是改账。

    判据判据判据 —— 这处和甲8-4 一样有**两条轴**，所以刻意**不用**
    ``pytest.raises`` 包起来。``set_opinion`` 外层有个 ``except Exception`` 把
    异常收成 ``{"ok": False, "detail": ...}``，修后根本不抛到用例这一层；而若
    先断言「有没有抛」，修前就会先在那条上炸掉，**危害那条断言一次也跑不到**，
    也就无从证明它自己有射程。改成把两条轴收进 problems，危害在前：
        危害轴：戳有没有被改写（修前 default|default，修后 u_a|bank_a 原封不动）
        静默轴：返回值有没有说实话（修前 ok=True，修后 ok=False 且带真原因）

    判据里写全 ``SELECT user_id, bank_id FROM facts``，不能只写 ``user_id``：
    upsert 那条 ``INSERT INTO opinions (... user_id, bank_id ...)`` 里也有
    ``user_id``，判据一宽就会打在写入语句上——修前也红，但红在别处（假红）。

    账本那一笔（``record_event`` 的 ``opinion_set``）会跟着盖同一个错戳，这里
    刻意**不**断言它：``record_event`` 外面套着 ``except Exception → debug``，
    它不写成功也不吭声，在这上面断言绿了也证明不了账本真被写对过。
    """
    import ducky.opinion as opinion_mod

    fid = _seed_fact(facts_db, "identity", "k_a1", "甲库事实", "u_a", "bank_a")
    src = "jia8-7-probe"

    # opinions 是跨用例长存的表（本文件的 _fresh_db 只清 core_memory/memory_banks），
    # 先把这个 source 的残留清掉，免得 upsert 打在别的用例留下的行上。
    opinion_mod.ensure_opinion_schema()
    facts_db.execute("DELETE FROM opinions WHERE source=?", (src,))
    facts_db.commit()

    def _stamp():
        row = facts_db.execute(
            "SELECT user_id, bank_id, stance FROM opinions WHERE fact_id=? AND source=?",
            (fid, src),
        ).fetchone()
        return None if row is None else (row["user_id"], row["bank_id"], row["stance"])

    # 第一拍：库好着的时候写，戳必须跟着事实走
    first = opinion_mod.set_opinion(fid, "support", confidence=0.7, source=src)
    assert first["ok"] is True, f"正常路径写信念就不该失败: {first}"
    assert _stamp() == ("u_a", "bank_a", "support"), (
        f"前置条件没建立起来，后面的「改写」就无从谈起: {_stamp()}"
    )

    # 第二拍：只锁住 _fact_scope 查 facts 那一句，同 (fact_id, source) 再写
    monkeypatch.setattr(
        opinion_mod, "get_facts_conn",
        lambda: _SelectiveFailConn(
            facts_db, lambda sql: "SELECT user_id, bank_id FROM facts" in sql),
    )
    second = opinion_mod.set_opinion(fid, "oppose", confidence=0.3, source=src)

    problems = []
    after = _stamp()
    if after is None or after[:2] != ("u_a", "bank_a"):
        problems.append(
            f"一次瞬时锁库把已盖对的作用域戳永久改写了：('u_a', 'bank_a') → {after}"
        )
    if second.get("ok") is True:
        problems.append(f"锁库被当成了正常业务结果，返回 {second}")
    assert not problems, "；".join(problems)

    # 修后：那个诚实出口本来就在，得确认它说的是真原因而不是又吞一层
    assert "locked" in str(second.get("detail", "")).lower(), (
        f"降级挡住了，但没把真原因交出来: {second}"
    )


# ── 甲11：一次写入不许把别的库的记忆「标死」 ───────────────────────────
#
# 这一条的后果形状与前面八处都不一样：前面是**读**漏或**戳**错，这一条是
# 往别人家的账本上写一笔「此条已作废」。``track_knowledge_evolution`` 的检索
# 原来是 ``filters={"user_id": user_id}``，不带 bank；同一函数体里另外五个兄弟
# 调用全都透传了 bank_id，只有它和它的两个调用点漏了——是漏项，不是设计。
#
# 危害链条要完整地看，不然会低估：往 A 库写一条文本 → 检索捞到 B 库一条共用
# 中文词的记忆 → 判成 replaces → 把 B 那条写进 ``memory_states.state=
# 'superseded'`` → ``recall_funnel`` 随后按这张表把它从召回结果里剔掉。于是
# **A 库的一次写入，让 B 库一条好端端的记忆从此召回不到**，而 B 库的数据一个
# 字节都没动、日志里也没有任何一行说它被动过。生产目前只跑一个库，缺陷在位
# 但还没打响。
#
# 判据轴（比用例本身更容易搞错）：
#   · 危害轴（红）：B 库那条**不许**被标 superseded，也不许出现配对行
#   · 过紧轴（修前修后都得绿）：默认域的 **v19 存量点（压根没有 bank_id
#     字段）必须照旧能被标死**——这一条不是凑数的。修这个缺陷最顺手的写法是
#     把 ``bank_id=default`` 直接下推给 mem0，那样 Qdrant 的 must 语义会把所有
#     缺字段的存量点判为不匹配，**全域召回归零**，比原缺陷严重得多。这条断言
#     是拦住那种「看起来更严谨」的修法的唯一一道岗。
#
# 刻意**不给** ``track_knowledge_evolution`` 传 bank_id：修前那个函数没有这个
# 形参，传了会先死在 TypeError 上——红在签名轴而不是隔离轴，危害断言一次也跑
# 不到（甲8-4 / 甲8-7 已经栽过这一跤）。不传，靠修后的默认值兜住，同一句调用
# 修前红修后绿，而且它本身就是默认域调用方的真实长相。


_JIA11_USER = "jia11-user"
_JIA11_BANK_B = "jia11-bank-b"
_JIA11_LEGACY_ID = "jia11-legacy-default-point"
_JIA11_BANK_B_ID = "jia11-named-bank-b-point"
_JIA11_NEW_ID = "jia11-new-default-write"


class _MustSemanticsStore:
    """一个如实照 Qdrant **must 语义**过滤的假向量库。

    绝不用 MagicMock：假库一旦什么都答应，就分不清「代码把域过滤下推了」和
    「代码压根没过滤」——而这正是本条要区分的东西。must 语义的关键是**缺字段
    即不匹配**：payload 里没有 ``bank_id`` 这个字段的点，被 ``bank_id=?`` 条件
    直接判为不匹配（bank_contract 顶部记了实测数据）。这条性质是整个两半契约
    的由来，假库必须忠实复现，否则用例会在一个比生产宽松的世界里通过。
    """

    def __init__(self, points):
        self._points = points
        self.filters_seen: list[dict] = []

    @staticmethod
    def _field(point, key):
        """取字段：顶层优先，其次 metadata——缺就返回哨兵，代表「无此字段」。"""
        if key in point:
            return point[key]
        meta = point.get("metadata")
        if isinstance(meta, dict) and key in meta:
            return meta[key]
        return _MISSING

    def search(self, query, filters=None, limit=5):
        self.filters_seen.append(dict(filters or {}))
        hits = []
        for point in self._points:
            if all(self._field(point, k) == v for k, v in (filters or {}).items()):
                hits.append(dict(point))
        return {"results": hits[:limit]}


_MISSING = object()


def test_jia11_a_default_write_must_not_supersede_another_banks_memory():
    """默认域写一条，**不许**把命名库 B 的记忆标成 superseded。

    ⚠️ 这条守的是「改错别人家的账」，不是一次报错。

    对照选 A=默认域 / B=命名库，是刻意的：默认域下 ``vector_scope_filters``
    **故意不下推** bank_id（下推即召回归零），所以本域的隔离全靠 Python 侧那次
    复筛顶着——**复筛是唯一承重的那一半**。生产跑的正是默认域，这个配对既是
    最险的，也是最真实的。

    ``memory_states`` / ``knowledge_evolution`` 两张表**没有也不该有**作用域列：
    它们是全局平表，只要「生成行的那次检索」按域收敛，表里就不可能出现跨库
    配对。本条就是这条不变量的守卫——别为了「看起来更严谨」去给表加列。
    """
    from ducky.utils import ensure_evolution_tables, get_facts_conn
    from ducky.layer1_selfcheck import track_knowledge_evolution

    ensure_evolution_tables()

    # 这两张表是长命表，autouse 的 _fresh_db 不动它们；自己的行自己收尾，
    # 免得和别的用例互相污染。
    ids = (_JIA11_LEGACY_ID, _JIA11_BANK_B_ID, _JIA11_NEW_ID)
    q = ",".join("?" * len(ids))
    conn = get_facts_conn()
    conn.execute(f"DELETE FROM memory_states WHERE memory_id IN ({q})", ids)
    conn.execute(
        f"DELETE FROM knowledge_evolution WHERE source_id IN ({q}) OR target_id IN ({q})",
        ids + ids,
    )
    conn.commit()
    conn.close()

    # 两个点共用中文词「围棋」——产品的共同话题检测用的是
    # ``re.findall(r'[一-鿿]{2,}')``，匹配的是**极大连续中文串**而不是
    # bigram，所以两条文本必须共有一段被非中文字符界定的完整中文串。
    store = _MustSemanticsStore([
        # v19 存量点：顶层和 metadata 里**都没有** bank_id 字段。
        {"id": _JIA11_LEGACY_ID, "memory": "围棋 is the old default hobby",
         "user_id": _JIA11_USER},
        # 命名库 B 的点：bank 戳在 metadata 里（写侧唯一能盖戳的通道）。
        {"id": _JIA11_BANK_B_ID, "memory": "围棋 is bank B private hobby",
         "user_id": _JIA11_USER, "metadata": {"bank_id": _JIA11_BANK_B}},
    ])

    # 「现在是」在 replaces_keywords 里（注意 stop_topics 里是「现在」，不是
    # 「现在是」，所以共同话题不会被它抵消）→ 判定必然落到 replaces → 走标死。
    track_knowledge_evolution(
        store, _JIA11_USER, "围棋 现在是 basketball", _JIA11_NEW_ID
    )

    conn = get_facts_conn()
    # 判据 SQL 照抄 recall_funnel 自己那一句——断言要对着**用户真能看见的
    # 后果**，不是对着一张中间表的形状。
    rows = conn.execute(
        f"SELECT memory_id FROM memory_states WHERE memory_id IN ({q}) "
        "AND state = 'superseded'",
        ids,
    ).fetchall()
    superseded = {r[0] for r in rows}
    paired = conn.execute(
        "SELECT relation_type FROM knowledge_evolution WHERE source_id=? AND target_id=?",
        (_JIA11_BANK_B_ID, _JIA11_NEW_ID),
    ).fetchall()
    conn.close()

    problems = []
    # 危害轴放最前面：两条轴挤在一个用例里时，先断言的那条才有射程。
    if _JIA11_BANK_B_ID in superseded:
        problems.append(
            "默认域的一次写入把命名库 B 的记忆标成了 superseded，"
            "recall_funnel 从此会把它从召回里剔掉（B 库数据一个字节没动、"
            "日志一行没留）"
        )
    if paired:
        problems.append(
            f"knowledge_evolution 里出现了跨库配对行 "
            f"{_JIA11_BANK_B_ID[:12]}→{_JIA11_NEW_ID[:12]}: "
            f"{[r[0] for r in paired]}"
        )
    # 过紧轴：修前修后都必须绿。它红了说明修法把 bank_id=default 下推了，
    # 那是比原缺陷严重得多的全域召回归零。
    if _JIA11_LEGACY_ID not in superseded:
        problems.append(
            "默认域的 v19 存量点（无 bank_id 字段）没能被标死——域过滤收得过紧，"
            "缺字段的存量数据被当成了「别的库」，正常演化被打断"
        )
    assert not problems, "；".join(problems)

    # 默认域**故意不下推** bank_id。这一句修前修后都绿，留着是当护栏：
    # 哪天有人图省事把它加上，这里会立刻发红，而不是等到生产召回归零。
    assert store.filters_seen, "检索压根没发生，上面的断言就都不算数"
    assert "bank_id" not in store.filters_seen[0], (
        f"默认域把 bank_id 下推给了向量库，缺字段的 v19 存量点会被 must 语义"
        f"全部滤掉，召回归零: {store.filters_seen[0]}"
    )


class _NoopMemory:
    """够驱动 ``layer1_add_wrapper`` 走完一趟的最小假库。

    ``add`` 返回 None 是刻意的：``_index_after_add`` 头一句就是
    ``if add_result is None: return``，于是显著性登记、FTS 索引、六型分类
    全部不进场——本条要量的是「调用点有没有把域传下去」，不该顺带把半个
    写链拖进来。
    """

    def __init__(self, search_results):
        self._search_results = search_results
        self.updated: list[tuple] = []

    def search(self, query, filters=None, limit=3):
        return {"results": list(self._search_results)}

    def get_all(self, filters=None, limit=10000):
        return {"results": []}

    def add(self, messages, user_id=None, metadata=None, infer=True):
        return None

    def update(self, memory_id, text, metadata=None):
        self.updated.append((memory_id, text))
        return None


def test_jia11_both_add_wrapper_call_sites_pass_the_bank_into_evolution_tracking(monkeypatch):
    """``layer1_add_wrapper`` 的**两个**调用点都得把 bank 透传下去。

    修法是「一处签名 + 两处透传」。只验签名不验调用点，等于只修了一半还发绿：
    形参有默认值 ``default``，调用点漏传时函数照样跑，只是把每一次写入都当成
    默认域的写入——正是缺陷本身的行为。所以两个出口都要点名。

    ``infer=False`` 让 Step 0 的 LLM self-edit 整段跳过，两条路都变成纯规则、
    可复现，也不烧 token。
    """
    import ducky.layer1_selfcheck as l1

    seen: list[dict] = []

    def _recorder(memory, user_id, new_text, new_id="new_item", **kwargs):
        seen.append(dict(kwargs))

    monkeypatch.setattr(l1, "track_knowledge_evolution", _recorder)

    bank = "jia11-bank-a"
    text = "围棋 现在是 basketball"

    # 出口一：新增路径（去重不命中 → 容量检查 → 写入前演化追踪）
    new_path = _NoopMemory(search_results=[])
    l1.layer1_add_wrapper(new_path, [{"role": "user", "content": text}],
                          user_id=_JIA11_USER, metadata={}, bank_id=bank, infer=False)

    # 出口二：去重更新路径。要让 dedup_check 真的命中，得同时过三道：
    # 命名域会下推 bank_id（所以点上要带戳）、复筛要过、文本相似度 > 0.85
    # （用同一段文本，相似度 1.0）。
    dup_path = _NoopMemory(search_results=[
        {"id": "jia11-existing-id", "memory": text, "score": 0.99, "bank_id": bank},
    ])
    l1.layer1_add_wrapper(dup_path, [{"role": "user", "content": text}],
                          user_id=_JIA11_USER, metadata={}, bank_id=bank, infer=False)

    problems = []
    if len(seen) != 2:
        problems.append(
            f"预期两个调用点各触发一次演化追踪，实际 {len(seen)} 次"
            f"（去重更新路径是否真的命中：updated={dup_path.updated}）"
        )
    for i, kwargs in enumerate(seen):
        if kwargs.get("bank_id") != bank:
            problems.append(
                f"第 {i + 1} 个调用点没把域传下去：bank_id={kwargs.get('bank_id')!r}"
                f"（期望 {bank!r}）——演化追踪会拿默认域去检索，跨库标死照旧发生"
            )
    assert not problems, "；".join(problems)


# ═══════════════════════════════════════════════════════════════════════
# 甲6 · /add/raw 的 facts 登记 bank-blind —— 跨库静默丢写 + 错戳
# ═══════════════════════════════════════════════════════════════════════

_JIA6_USER = "jia6-tenant"
_JIA6_TEXT = "甲6 原味抽屉跨库登记用例的固定原文。两次写入必须字节一致，才凑得出同一个内容哈希。"


def _jia6_raw_key(content: str) -> str:
    """复刻 ``/add/raw`` 自己算 fact_key 的那两行（``raw_drawer.py`` :44-50）。

    刻意**不硬编码哈希字面量**：走产品自己的注入清洗函数、同一个哈希口径、
    同一个截断长度。清洗规则或哈希口径哪天改了，这个用例跟着改，
    不会留下一个「键对不上但用例照旧发绿」的假绿。
    """
    import hashlib

    from ducky.security.injection_guard import validate_and_sanitize_memory_content

    _ok, sanitized, _rejection = validate_and_sanitize_memory_content(content.strip())
    return f"raw:{hashlib.sha256(sanitized.encode()).hexdigest()[:16]}"


class _Jia6Memory:
    """``/add/raw`` 向量半边的最小替身。

    ``add`` 返回 None 就够了 —— 本组用例量的是 facts 表那一行的作用域戳，
    向量侧不该进场。**不用 MagicMock**：宽容替身会把「方法名写错」这类
    真缺陷一并吞掉。
    """

    def __init__(self):
        self.added: list[dict] = []

    def add(self, content, user_id=None, metadata=None, infer=True):
        self.added.append({"user_id": user_id, "metadata": dict(metadata or {})})
        return None


@pytest.fixture
def jia6_client(monkeypatch, facts_db):
    """只挂了 ``/add/raw`` 的 app + 一张由产品自己迁出来的 facts 表。

    FTS 半边整段短路：``raw_drawer`` 会往 text_fts 库写索引，本组不量那一半，
    也不想让 memories 表被搅进来（``/add/raw`` 的去重查询就在那张表上）。
    """
    import ducky.hot.raw_drawer as rd
    import ducky.mem0_runtime as rt
    import ducky.text_fts as tf
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(rt, "get_memory", lambda: _Jia6Memory())
    monkeypatch.setattr(tf, "_index_memory", lambda *a, **k: None)

    app = FastAPI()
    rd.register_raw_drawer_routes(app)
    return TestClient(app), facts_db


def test_jia6_same_content_in_two_banks_must_not_silently_lose_the_second_write(jia6_client):
    """同一租户在两个库写同一段原文，facts 必须留下**两行各盖自己戳**。

    修复前：``INSERT`` 只给了 ``agent_id``，作用域两列一个字没给，落到迁移时的
    ``DEFAULT 'default'``。于是两次写入凑出的唯一元组
    ``(agent_id, user_id, bank_id, category, fact_key)`` 一模一样，
    ``INSERT OR IGNORE`` 把后写的那条**静默丢掉** —— 不抛异常、连一句 debug 都
    没有，而响应照旧报 ``facts_registered: true``。两重后果：

      · **静默丢写**：库 B 的原文在 facts 层根本不存在，用户以为存进去了；
      · **错戳**：留下的那一行盖着 ``default|default``，分库统计/删除/审计
        会把库 A 的内容记到默认域名下。

    跨**租户**撞车早就被 ``agent_id=req.user_id`` 挡住了（v19.4.0 P0-2b 修的
    就是那一半）；bank 是 v20 新加的维度，同一个坑又露了另外半边。
    """
    client, conn = jia6_client
    bank_a, bank_b = "jia6-bank-a", "jia6-bank-b"

    r_a = client.post("/add/raw", json={
        "content": _JIA6_TEXT, "user_id": _JIA6_USER,
        "bank_id": bank_a, "dedup": False,
    })
    r_b = client.post("/add/raw", json={
        "content": _JIA6_TEXT, "user_id": _JIA6_USER,
        "bank_id": bank_b, "dedup": False,
    })

    problems = []
    for name, resp in ((bank_a, r_a), (bank_b, r_b)):
        if resp.status_code != 200:
            problems.append(f"库 {name} 的 /add/raw 返回 {resp.status_code}")

    rows = conn.execute(
        "SELECT agent_id, user_id, bank_id, fact_key, memory_tier FROM facts "
        "WHERE fact_key LIKE 'raw:%' ORDER BY bank_id"
    ).fetchall()
    stamped = {(r["user_id"], r["bank_id"]) for r in rows}

    # ① 危害先说：丢了几行。
    if len(rows) != 2:
        problems.append(
            f"同一租户在两个库写同一段原文，facts 只落了 {len(rows)} 行（期望 2 行）"
            f"——唯一约束里的作用域两列没给，后写的那条被 INSERT OR IGNORE "
            f"静默丢掉；实际落地的戳={sorted(stamped)}"
        )

    # ② 再说错戳，以及响应替一次没发生的登记作证。
    for bank, resp in ((bank_a, r_a), (bank_b, r_b)):
        landed = (_JIA6_USER, bank) in stamped
        claimed = resp.status_code == 200 and resp.json().get("facts_registered") is True
        if not landed:
            extra = (
                "；而响应照旧报 facts_registered=true —— 响应在替一次没有发生的"
                "登记作证，这比只丢数据更糟：日志和返回值都在说存进去了"
                if claimed else ""
            )
            problems.append(f"库 {bank} 的原文没有留下属于自己的 facts 行{extra}")

    # ③ agent_id 按租户落（v19.4.0 P0-2b 修的那一半不许回退）。
    for r in rows:
        if r["agent_id"] != _JIA6_USER:
            problems.append(
                f"agent_id 没按租户落：{r['agent_id']!r}（期望 {_JIA6_USER!r}）"
                f"——跨租户撞车那半边会跟着塌回去"
            )
        if r["memory_tier"] != "verbatim":
            problems.append(f"原味行的 memory_tier 变了：{r['memory_tier']!r}")

    # ④ 键形状钉死：作用域**不许**再塞进 fact_key。
    expected_key = _jia6_raw_key(_JIA6_TEXT)
    for r in rows:
        if r["fact_key"] != expected_key:
            problems.append(
                f"fact_key 形状变了：{r['fact_key']!r}（期望全局形状 {expected_key!r}）。"
                f"计划书原文要求改成 raw:{{user}}:{{bank}}:{{hash}}，实施时否掉了："
                f"作用域已经由 user_id/bank_id 两列进了唯一约束，键里再塞一遍是冗余，"
                f"而且一改形状，存量库里 raw:{{hash}} 的老行在升级后会被判成新键再插"
                f"一行，OR IGNORE 的幂等去重当场失效"
            )

    assert not problems, "；".join(problems)


def test_jia6_legacy_global_key_row_still_deduplicates(jia6_client):
    """存量库里 ``raw:{hash}`` 形状的老行，升级后同一段内容仍然只有一行。

    这一条**修复前后都是绿的**，它不是用来证明 甲6 修好了 —— 它是用来挡住
    一种**错的修法**。计划书 甲6 那一行原文要求把 fact_key 改成
    ``raw:{user}:{bank}:{hash}``。真那么改，生产库里所有老形状的键就再也撞不
    上新键：同一段老内容会在升级后被当成新事实再插一行，幂等去重当场失效，
    而且是静默的、按库容量线性放大的 —— 「修复」自己变成了新缺陷。

    所以这条守的是「别把默认域的存量幂等改坏」。它红的时候，说明有人
    （包括未来的我）又把作用域塞进了键形状里。与 ``test_jia6_same_content_...``
    构成一对：那条是危害轴，这条是过紧轴。
    """
    client, conn = jia6_client
    key = _jia6_raw_key(_JIA6_TEXT)

    # 照 v19 的 /add/raw 在默认域会落下的样子造一行老数据。唯一元组的五个字段
    # 一个都不能差，差一个这条用例就失去意义：
    #   agent_id  —— 老码写的是 req.user_id，默认域即 DEFAULT_USER_ID
    #   user_id / bank_id —— 老码一个字没给，落到迁移补列时的 DEFAULT 'default'
    #   category / memory_tier —— 原味轨道都是 'verbatim'
    from ducky.bank_contract import DEFAULT_BANK_ID
    from ducky.utils import DEFAULT_USER_ID

    conn.execute(
        """INSERT INTO facts (category, fact_key, fact_value, source, memory_tier,
                              agent_id, user_id, bank_id, archived,
                              created_at, updated_at)
           VALUES ('verbatim', ?, ?, 'raw_drawer', 'verbatim', ?, ?, ?, 0,
                   '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
        (key, _JIA6_TEXT[:500], DEFAULT_USER_ID, DEFAULT_USER_ID, DEFAULT_BANK_ID),
    )
    conn.commit()

    resp = client.post("/add/raw", json={
        "content": _JIA6_TEXT, "user_id": DEFAULT_USER_ID,
        "bank_id": DEFAULT_BANK_ID, "dedup": False,
    })

    problems = []
    if resp.status_code != 200:
        problems.append(f"/add/raw 返回 {resp.status_code}")

    rows = conn.execute(
        "SELECT user_id, bank_id, fact_key FROM facts WHERE fact_key LIKE 'raw:%'"
    ).fetchall()
    if len(rows) != 1:
        problems.append(
            f"默认域同一段原文在存量老行之外又落了 {len(rows) - 1} 行（共 {len(rows)} 行，"
            f"键={sorted({r['fact_key'] for r in rows})}）——老键撞不上新键，"
            f"幂等去重失效"
        )

    assert not problems, "；".join(problems)


# ── 甲12：全库 trust_score 腰斩 —— 只拦写，不动读 ─────────────────────

_JIA12_A_USER, _JIA12_A_BANK = "u_jia12_a", "jia12-bank-a"
_JIA12_B_USER, _JIA12_B_BANK = "u_jia12_b", "jia12-bank-b"


def _jia12_seed(conn, category, key, value, trust, user_id, bank_id, entity):
    """种一条事实并挂一个实体 —— v2 靠实体重叠度配对，没实体就一对也配不出来。"""
    conn.execute(
        "INSERT INTO facts (category, fact_key, fact_value, trust_score, user_id, bank_id, "
        "archived, created_at, updated_at) VALUES (?,?,?,?,?,?,0,?,?)",
        (category, key, value, trust, user_id, bank_id,
         time.strftime("%Y-%m-%dT%H:%M:%S"), time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    fact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO entities (name, entity_type) VALUES (?,'service')", (entity,))
    ent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO fact_entities (fact_id, entity_id) VALUES (?,?)", (fact_id, ent_id))
    conn.commit()
    return fact_id


@pytest.fixture
def jia12_client(monkeypatch, facts_db):
    """挂了 legacy 路由的 app，两组「会被判成互相矛盾」的事实各在自己域里。

    ⚠️ 这里必须 patch ``ducky.hot.legacy_routes._get_facts_conn``，**不能** patch
    ``legacy_helpers`` 上的同名函数：``legacy_routes:25`` 是模块级 ``from ... import``，
    早就把名字绑到自己模块上了，改上游那个对它一点影响都没有 —— 端点会照旧连**真
    的生产 facts 库**，用例要么假绿，要么当场污染生产数据。这正是 S3 那道「同名
    导入」AST 守卫要抓的东西。
    """
    import ducky.hot.legacy_helpers as lh
    import ducky.hot.legacy_routes as lr
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(lr, "_get_facts_conn", lambda: lh._get_db(_DB))

    ids = {
        # B 域自成一对：分低的那条（0.8）是 v1/v2 都会去腰斩的那条。
        # A 域身份调用时，它一分都不许动。
        "b_high": _jia12_seed(facts_db, "jia12_deploy_b", "k_b1", "网关部署成功",
                              0.9, _JIA12_B_USER, _JIA12_B_BANK, "beta_svc"),
        "b_low":  _jia12_seed(facts_db, "jia12_deploy_b", "k_b2", "网关部署失败",
                              0.8, _JIA12_B_USER, _JIA12_B_BANK, "beta_svc"),
        # A 域自成一对（**另一个 category、另一个实体名**，免得跟 B 域串起来）：
        # 这是「过紧轴」的伴生断言 —— 护栏不许顺手把自己域的活儿也挡掉。
        "a_high": _jia12_seed(facts_db, "jia12_deploy_a", "k_a1", "索引升级成功",
                              0.7, _JIA12_A_USER, _JIA12_A_BANK, "alpha_svc"),
        "a_low":  _jia12_seed(facts_db, "jia12_deploy_a", "k_a2", "索引升级失败",
                              0.6, _JIA12_A_USER, _JIA12_A_BANK, "alpha_svc"),
    }

    app = FastAPI()
    lr.register_legacy_routes(app)
    return TestClient(app), facts_db, ids


def _jia12_trust(conn, fact_id):
    return conn.execute("SELECT trust_score FROM facts WHERE id=?", (fact_id,)).fetchone()[0]


def test_jia12_contradiction_v2_must_not_halve_another_banks_trust_score(jia12_client):
    """甲12 写点一：``/prune/contradiction-v2`` 带 ``dry_run=false`` 调一次，
    别的域的事实 ``trust_score`` 必须**一分不变**。

    ⚠️ 这条守的是一次**不可逆**的数据毁坏，不是一次报错。

    修复前：读是全库无域的，UPDATE 也没有任何作用域条件。于是 B 域自己的两条
    事实被配成一对，分低的那条被腰斩到原值的一半 —— 而发起调用的是 A 域，它既
    没有 B 域的凭据，也从来没被要求出示过：端点根本不看域。降权没有反向操作，
    这一刀砍下去就回不来了，B 域后续检索的排序权重被永久拉低。

    本轮**只拦写、不动读**（读侧随甲5 推迟到丙9），所以响应里照旧会**看见** B
    域那一对 —— 用例因此不去断言「看不见」，只断言「没动过」，并且要求端点把挡
    掉的行数如实记账（``skipped_out_of_scope``）：读还是全库的时候，一个不记账
    的响应就是在替一次没有发生的降权作证。

    两条轴一次报出（刻意不用 ``pytest.raises``）：
      · 危害轴 —— B 域那条分数不许变（修前必红）；
      · 过紧轴 —— A 域自己那条**必须**照旧被腰斩（护栏不许把本域的活儿也挡掉）。
    """
    client, conn = jia12_client[0], jia12_client[1]
    ids = jia12_client[2]

    b_low_before = _jia12_trust(conn, ids["b_low"])
    a_low_before = _jia12_trust(conn, ids["a_low"])

    r = client.post("/prune/contradiction-v2", params={
        "dry_run": "false", "user_id": _JIA12_A_USER, "bank_id": _JIA12_A_BANK,
    })

    problems = []
    if r.status_code != 200:
        problems.append(f"/prune/contradiction-v2 返回 {r.status_code}")
    else:
        body = r.json()
        b_low_after = _jia12_trust(conn, ids["b_low"])
        a_low_after = _jia12_trust(conn, ids["a_low"])

        # 危害轴：跨域腰斩
        if b_low_after != b_low_before:
            problems.append(
                f"B 域事实被 A 域的一次调用腰斩：{b_low_before} → {b_low_after}"
                f"（不可逆）"
            )
        # 过紧轴：本域的降权必须照旧发生，否则护栏是靠「谁都不干」通过的
        if a_low_after >= a_low_before:
            problems.append(
                f"A 域自己那条没被降权：{a_low_before} → {a_low_after}"
                f"（护栏过紧，端点被废掉了）"
            )
        # 记账轴：挡掉了就得说挡掉了
        if body.get("skipped_out_of_scope", 0) < 1:
            problems.append(
                f"跨域的那一行被挡掉了却没上报："
                f"skipped_out_of_scope={body.get('skipped_out_of_scope')!r}，"
                f"audited={body.get('audited')!r}"
            )

    assert not problems, "；".join(problems)


def test_jia12_contradiction_v1_must_not_halve_another_banks_trust_score(jia12_client):
    """甲12 写点二：``/prune/contradiction``（v1）同一条缺陷、同一条断言。

    v1 比 v2 更容易撞上：它连实体重叠都不用算，同 category 下两条事实的正文
    命中一组 ``CONTRADICTION_WORDS``（这里是「成功 / 失败」）就动手。

    额外守一个**撒谎的计数器**：v1 原先在 ``execute`` 之后**无条件** ``audited += 1``，
    所以哪怕一行都没落地，响应也会报「审计了 N 条」。日志本身在撒谎比没有日志更糟，
    这条用例要求 ``audited`` 只数真落地的行。
    """
    client, conn = jia12_client[0], jia12_client[1]
    ids = jia12_client[2]

    b_low_before = _jia12_trust(conn, ids["b_low"])
    a_low_before = _jia12_trust(conn, ids["a_low"])

    r = client.post("/prune/contradiction", params={
        "dry_run": "false", "user_id": _JIA12_A_USER, "bank_id": _JIA12_A_BANK,
    })

    problems = []
    if r.status_code != 200:
        problems.append(f"/prune/contradiction 返回 {r.status_code}")
    else:
        body = r.json()
        b_low_after = _jia12_trust(conn, ids["b_low"])
        a_low_after = _jia12_trust(conn, ids["a_low"])

        if b_low_after != b_low_before:
            problems.append(
                f"B 域事实被 A 域的一次调用腰斩：{b_low_before} → {b_low_after}"
                f"（不可逆）"
            )
        if a_low_after >= a_low_before:
            problems.append(
                f"A 域自己那条没被降权：{a_low_before} → {a_low_after}"
                f"（护栏过紧，端点被废掉了）"
            )
        if body.get("skipped_out_of_scope", 0) < 1:
            problems.append(
                f"跨域的那一行被挡掉了却没上报："
                f"skipped_out_of_scope={body.get('skipped_out_of_scope')!r}"
            )
        # audited 必须只数真落地的行：本域 1 行落地、跨域 1 行被挡 → 恰好 1
        if body.get("audited") != 1:
            problems.append(
                f"audited 在替没落地的行作证：audited={body.get('audited')!r}，"
                f"期望 1（只有 A 域那一行真落了）"
            )

    assert not problems, "；".join(problems)


# ── 甲13：同一次 /add 的两半不许落进不同的域；删除的两半作用域必须对称 ──
#
# 缺陷①（正在漏）：`store_verbatim(..., bank_id=...)` 改前的域判据是
#   `if bank_id == DEFAULT_BANK_ID and md.get("bank_id")`
# 旁边的注释写着「显式参数优先」，代码做的正好相反 —— `AddRequest.bank_id`
# 带默认值 "default"（api_models.py），且 /add 在调用它之前先执行
# `md.setdefault("bank_id", req.bank_id)`（hot/add.py:80），于是第二个合取项
# **恒为真**，整条判据塌缩成 `bank_id == DEFAULT_BANK_ID`。
# 后果不是「写进了没指名的库」这么轻：客户端只在 metadata 里指名域时，
# **同一次 /add 的两半会落进不同的域** —— 事实/向量/显著度/FTS 跟参数走，
# 原文跟 metadata 走。原文成了孤儿，而 verbatim_search 按 bank 严格过滤，
# 写的人拿自己一直在用的那个域，永远搜不到自己刚写下的那句话。
#
# 缺陷②（潜伏）：`_delete_turn_ids` 改前只有主表那条 DELETE 带作用域谓词，
# FTS 清理是**无条件**删调用方传进来的全部 id —— 主表命中 0 行时索引照删，
# 结果是别的域的对话还在表里、却从检索里消失了（verbatim_search 只走 FTS）。
# 它的 docstring 当时已经在拿「safe for future callers that receive ids from
# an untrusted request」当既有性质用，而 FTS 那一半并不具备它 —— 与甲12 那个
# 替没落地的行作证的计数器同族：**注释在替代码撒谎**。当前三个调用方都先按域
# 取 id，此路不可达，故它是潜伏缺陷，修的是这句承诺与「两半不对称」。

_JIA13_USER = "jia13_writer"
_JIA13_TEXT = "甲十三号原文落域核验句"
_JIA13_QUERY = "原文落域核验"


class _Jia13StubMemory:
    """够 /add 主链跑完的最小替身。

    `llm` / `config` 必须是 None —— `patch_llm_for_speed(mem)` 靠它们判空后
    直接返回。这里不用 MagicMock：宽容替身会让「产品代码调了个不存在的方法」
    也照样绿。
    """

    llm = None
    config = None

    def add(self, *a, **kw):
        return {"results": []}

    def search(self, *a, **kw):
        return {"results": []}

    def get_all(self, *a, **kw):
        return []


def _jia13_reset_verbatim():
    """把原文层三张表清成不存在；建表交给产品代码自己做（绝不手写 DDL）。

    `ensure_verbatim_schema()` 没有模块级「已建过」标志，每次调用都真跑一遍
    建表迁移，所以 DROP 之后产品代码会自愈。
    """
    fconn = sqlite3.connect(_DB)
    fconn.execute("DROP TABLE IF EXISTS verbatim_turns")
    fconn.commit()
    fconn.close()
    tconn = sqlite3.connect(_TEXT_DB)
    for stmt in (
        "DROP TRIGGER IF EXISTS verbatim_ai",
        "DROP TRIGGER IF EXISTS verbatim_ad",
        "DROP TRIGGER IF EXISTS verbatim_au",
        "DROP TABLE IF EXISTS verbatim_fts",
        "DROP TABLE IF EXISTS verbatim_fts_map",
    ):
        try:
            tconn.execute(stmt)
        except Exception:
            pass
    tconn.commit()
    tconn.close()


def _jia13_verbatim_rows():
    """直接读主表 —— 这是本组用例的**主判据轴**。

    不走 verbatim_search 当主判据：它整个函数体裹在 try/except 里返回 []
    （干净降级），一条「搜不到」的断言可能红在别的轴上（建表失败、分词失配），
    那就是假红。主表 SQL 只可能红在「域写错了」这一条轴上。
    只按 content 过滤、不按 user_id：/add 会用 `_normalize_user_id` 重写
    user_id，把它写进断言等于把一条与本缺陷无关的映射规则也钉住了。
    """
    conn = sqlite3.connect(_DB)
    try:
        return conn.execute(
            "SELECT id, user_id, bank_id FROM verbatim_turns WHERE content=? ORDER BY id",
            (_JIA13_TEXT,),
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture()
def jia13_add_client(monkeypatch):
    """挂真实 /add 路由的 client + 事实侧收到的 bank_id 记录本。

    ⚠️ 护栏自己不许有能力改变结果：被替身接掉的东西（layer1 主链、冲突扫描、
    coalesce 注册与后台线程）在 hot/add.py 里全部排在 `store_verbatim`
    （:186）**之后**，替掉它们不可能把这条缺陷掩盖掉，也不可能替它作证。
    `store_verbatim` 之前那一段（作用域归一、bank 注册、load_speed_cfg、
    注入防御）一律保持真身。

    ⚠️ coalesce / 冲突扫描的 patch 必须打在**源模块**上：hot/add.py 是在函数体
    内 `from ducky.add_speed import …` / `from ducky.conflict_resolver import …`
    的，改 add_mod 上的同名属性一点用都没有 —— 这正是 S3 那道「同名导入」AST
    守卫要抓的东西的镜像。而 `get_memory` / `lazy_import_layer1` 是模块级导入
    （add.py:11-16），必须打在 add_mod 上。
    """
    import ducky.add_speed as speed_facade
    import ducky.conflict_resolver as conflict_mod
    import ducky.hot.add as add_mod
    import ducky.mem0_runtime as runtime_mod
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _jia13_reset_verbatim()

    stub = _Jia13StubMemory()
    monkeypatch.setattr(runtime_mod, "get_memory", lambda: stub)
    monkeypatch.setattr(add_mod, "get_memory", lambda: stub)

    facts_calls: list = []

    def _fake_layer1(mem_, msgs, uid, meta, *, bank_id=None, infer=True, **kw):
        facts_calls.append({"user_id": uid, "bank_id": bank_id})
        return {"status": "ok", "action": "stub_layer1"}

    monkeypatch.setattr(add_mod, "lazy_import_layer1", lambda: _fake_layer1)
    monkeypatch.setattr(
        conflict_mod, "scan_and_resolve_text_conflicts", lambda *a, **k: None
    )
    monkeypatch.setattr(speed_facade, "register_coalesce_flusher", lambda *a, **k: None)
    monkeypatch.setattr(speed_facade, "ensure_coalesce_worker", lambda *a, **k: None)

    app = FastAPI()
    add_mod.register_add_routes(app)
    return TestClient(app, raise_server_exceptions=False), facts_calls


def test_jia13_verbatim_must_land_in_the_same_bank_as_this_add_s_facts(jia13_add_client):
    """顶层不传 bank_id、只在 metadata 里指名域时，原文不许与事实分家。

    走的是真实 /add 路径（这条缺陷的活体现场就在那儿）。判据只有一条：
    **这一次 /add 的两半必须同域**。改前事实进 default、原文进 work。

    `force_sync` 必须放在 **metadata 里**：AddRequest 是 extra="allow"，顶层
    传它会落进 `__pydantic_extra__`，而 add.py:143 读的是 md，顶层那个字段
    是**惰性的**（test_v19_4_1_audit_fixes.py:552 顶层传了它却蒙对，是因为
    注入防御先抛了 400，根本没走到分支判断）。md.pop 在 :143、store_verbatim
    在 :186，所以这个开关不会污染被测的那份 metadata。
    """
    from ducky.bank_contract import DEFAULT_BANK_ID
    from ducky.verbatim_vault import verbatim_search

    client, facts_calls = jia13_add_client
    problems = []

    resp = client.post("/add", json={
        "messages": _JIA13_TEXT,
        "user_id": _JIA13_USER,
        # 顶层故意不传 bank_id —— Pydantic 会填默认值 "default"，
        # 「调用方没说」与「调用方明说 default」在此塌缩成同一件事。
        "metadata": {"bank_id": "work", "force_sync": True},
    })
    if resp.status_code != 200:
        problems.append(f"/add 返回 {resp.status_code}：{resp.text[:200]}")
    if not facts_calls:
        problems.append("没走到事实侧主链，两半同域的断言无从谈起")

    rows = _jia13_verbatim_rows()
    if len(rows) != 1:
        problems.append(f"原文行数应为 1（纯文本 payload 只产一轮），实为 {len(rows)}")

    if rows and facts_calls:
        verbatim_bank = rows[0][2]
        facts_bank = facts_calls[0]["bank_id"]
        # ① 危害轴：两半分家 —— 原文成孤儿，写的人搜不到自己刚写的话
        if verbatim_bank != facts_bank:
            problems.append(
                f"同一次 /add 的两半落进了不同的域：事实={facts_bank!r}、"
                f"原文={verbatim_bank!r} —— 原文成孤儿，写的人在自己那个域"
                f"永远搜不到自己刚写下的这句话"
            )
        # ② 方向轴：钉住「显式参数（含显式 default）说了算」，
        #    防止将来被改成「一律跟 metadata 走」而 ① 依旧同域为绿
        if verbatim_bank != DEFAULT_BANK_ID:
            problems.append(
                f"原文没跟显式参数走：期望 {DEFAULT_BANK_ID!r}，实为 {verbatim_bank!r}"
            )

    # ③ 用户可见轴：事实在哪个域，用户就该在那个域搜到自己的原文
    hits = verbatim_search(
        _JIA13_QUERY, user_id=_JIA13_USER, bank_id=DEFAULT_BANK_ID
    )
    if not any(_JIA13_TEXT in str(h.get("memory", "")) for h in hits):
        problems.append(
            f"事实所在的域（{DEFAULT_BANK_ID!r}）里搜不到刚写下的原文："
            f"verbatim_search 命中 {len(hits)} 条"
        )

    assert not problems, "；".join(problems)


def test_jia13_explicit_named_bank_still_puts_verbatim_in_that_named_bank(jia13_add_client):
    """过紧轴：顶层显式 bank_id="work" 时原文仍须落 work。

    防止把上一条修成「一律 default」—— 那样命名域的原文层就整个废了。
    设计上改前改后都绿：它守的不是缺陷，是修法的边界。
    """
    client, facts_calls = jia13_add_client
    problems = []

    resp = client.post("/add", json={
        "messages": _JIA13_TEXT,
        "user_id": _JIA13_USER,
        "bank_id": "work",
        "metadata": {"force_sync": True},
    })
    if resp.status_code != 200:
        problems.append(f"/add 返回 {resp.status_code}：{resp.text[:200]}")

    rows = _jia13_verbatim_rows()
    if len(rows) != 1:
        problems.append(f"原文行数应为 1，实为 {len(rows)}")
    elif rows[0][2] != "work":
        problems.append(
            f"顶层显式指名 work，原文却落在 {rows[0][2]!r} —— "
            f"命名域的原文层被修没了"
        )
    if facts_calls and facts_calls[0]["bank_id"] != "work":
        problems.append(
            f"事实侧也没进 work：{facts_calls[0]['bank_id']!r}（对照轴失效）"
        )

    assert not problems, "；".join(problems)


def test_jia13_out_of_scope_id_must_not_strip_another_bank_s_fts_row():
    """删除的两半必须作用域对称：越界 id 不许把别人的检索索引清掉。

    直接驱动 `_delete_turn_ids` —— 它没有模块外调用方，三个模块内调用方都先
    按域取 id，走 /add 或任何端点都到不了这个分支。所以按方案原定的「端点级
    负向对照」会在改前就是绿的（ids=[] → 早返回 0），那是**假绿**；能真红的
    只有直接驱动这一种。

    一条用例里正反两半都在：
      ① 危害轴：越界 id → 主表 rowcount==0 且该 id 的 FTS 映射**仍在**；
      ② 过紧轴：本域 id → 主表和 FTS 映射**两半都清掉**。
         少了 ② 的话，「FTS 那条 DELETE 干脆不执行」也能让 ① 变绿。
    """
    from ducky.bank_contract import DEFAULT_BANK_ID, make_scope
    from ducky.utils import get_facts_conn
    from ducky.verbatim_vault import _delete_turn_ids, store_verbatim

    problems = []
    _jia13_reset_verbatim()

    # 同一句话写进两个域：去重键含 bank_id，所以这是实打实的两行
    store_verbatim(_JIA13_USER, _JIA13_TEXT, {}, bank_id=DEFAULT_BANK_ID)
    store_verbatim(_JIA13_USER, _JIA13_TEXT, {}, bank_id="work")

    rows = _jia13_verbatim_rows()
    by_bank = {r[2]: r[0] for r in rows}
    if set(by_bank) != {DEFAULT_BANK_ID, "work"}:
        assert False, f"前置写入没铺好两个域的原文行：{rows!r}"

    def _fts_map_has(turn_id):
        conn = sqlite3.connect(_TEXT_DB)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM verbatim_fts_map WHERE turn_id=?", (turn_id,)
            ).fetchone()[0]
        finally:
            conn.close()

    work_id = by_bank["work"]
    default_id = by_bank[DEFAULT_BANK_ID]
    if not (_fts_map_has(work_id) and _fts_map_has(default_id)):
        assert False, "前置 FTS 映射没铺好，后面两条断言都无从谈起"

    fconn = get_facts_conn()

    # ① 危害轴：拿 default 域的作用域去删 work 域的 id
    deleted = _delete_turn_ids(fconn, [work_id], scope=make_scope(_JIA13_USER, DEFAULT_BANK_ID))
    if deleted != 0:
        problems.append(f"越界 id 竟从主表删掉了 {deleted} 行 —— 作用域谓词没生效")
    if _fts_map_has(work_id) != 1:
        problems.append(
            "越界 id 的 FTS 映射被清掉了：work 域那条对话还在表里、却从检索里"
            "消失了（verbatim_search 只走 FTS）—— 删除的两半作用域不对称"
        )

    # ② 过紧轴：本域 id 必须两半都清掉，否则 ① 可以靠「干脆不删 FTS」蒙绿
    deleted_in = _delete_turn_ids(fconn, [default_id], scope=make_scope(_JIA13_USER, DEFAULT_BANK_ID))
    if deleted_in != 1:
        problems.append(f"本域 id 没被删掉：rowcount={deleted_in}（护栏过紧）")
    if _fts_map_has(default_id) != 0:
        problems.append("本域 id 的 FTS 映射没清 —— 索引里留下了删不掉的幽灵")

    assert not problems, "；".join(problems)


# ── 甲14：一次不带分类的更新，不许把存量分类抹掉 ──────────────────────
#
# 缺陷形状和甲13 同源：一个默认值把「调用方没提」和「调用方明确说空」塌成了
# 同一件事。``_index_memory`` 是**先删再插**（DELETE-then-INSERT，函数里那句
# 注释自己写着「避免 REPLACE 残留」），所以一个空串不是「没写」，是**覆盖**；
# 而向量那半靠 mem0 的 payload merge 保住了自己那一份（先 deepcopy 再
# ``.update()``）—— 两半都在跑，但两半保留的信息量不同。这是两半契约的第三种
# 断法：不是一半没跑，也不是两半跑进不同的域，是两半记住的东西不一样多。
#
# ⚠️ 射程诚实记账：这条**目前是潜伏的**，不是用户当下看得见的故障。FTS 的
# 关键词读侧是一条死链 —— ``_hybrid_search`` 全仓零调用方，
# ``_bm25_keyword_search`` / ``_like_search`` 只在它内部被调，
# ``legacy_helpers.py`` 那三行是 v15.1 留的 re-export 兼容且无人从那儿取，
# ``/search`` 压根不碰 ``text_fts``。全仓读 ``memories.category`` 的 SQL 只有
# ``text_fts.py`` 自己那三条。所以按方案 §5.4 的尺子：修完之后用户能看见的
# 东西**没变**，但存量数据不再每次更新掉一次分类 —— 而那次丢失是不可逆的。
#
# 也正因为读侧是死的，下面四条一律**直读 SQL 行**，不经过任何搜索函数：走
# 搜索函数就是在测死代码，既能假绿也能假红（甲13 那条 ``verbatim_search``
# 的 try/except→``[]`` 已经教过一次）。

_JIA14_USER = "jia14_user"
_JIA14_CATEGORY = "偏好"


def _jia14_row(memory_id: str, user_id: str = _JIA14_USER, bank_id: str | None = None):
    """直读 FTS 主表那一行，返回 ``(content, category)``；没这一行返回 None。

    存储键按产品代码自己的口径推（``scoped_storage_key``），别在用例里假设
    「默认域就是裸 id」—— 那个等式成立是硬契约，但它是**别的**用例守的事。
    """
    from ducky.bank_contract import DEFAULT_BANK_ID, make_scope, scoped_storage_key

    scope = make_scope(user_id, bank_id or DEFAULT_BANK_ID)
    conn = sqlite3.connect(_TEXT_DB)
    try:
        return conn.execute(
            "SELECT content, category FROM memories WHERE id=? AND user_id=? AND bank_id=?",
            (scoped_storage_key(memory_id, scope), scope.user_id, scope.bank_id),
        ).fetchone()
    finally:
        conn.close()


def _jia14_seed(memory_id: str, content: str, category: str,
                user_id: str = _JIA14_USER, bank_id: str | None = None) -> None:
    """铺一条**带分类**的存量行，并当场验收铺没铺上。

    铺底走的是「显式传 category」那条路 —— 显式传值修前修后行为完全一致，
    所以铺底本身不会因为这次修复而变绿或变红，前置状态是干净的。
    """
    from ducky.bank_contract import DEFAULT_BANK_ID
    from ducky.text_fts import _index_memory, _init_text_fts

    _init_text_fts()
    _index_memory(memory_id, content, user_id=user_id, category=category,
                  bank_id=bank_id or DEFAULT_BANK_ID)
    row = _jia14_row(memory_id, user_id, bank_id)
    assert row is not None and row[1] == category, \
        f"前置铺底就没铺上（{row!r}），后面的断言无从谈起"


def test_jia14_update_must_not_erase_the_category_it_never_asked_about(monkeypatch):
    """负向对照（方案原文）：给一条带 category 的记忆做 ``/update``，
    断言 FTS 行的 category **仍在**。

    ``UpdateRequest`` 里根本没有 category 字段，所以调用方**没法**在请求里把
    分类补回来 —— 它只能被沿用，不能被重传。这就是「没说」和「说空」必须分开
    的原因：这个端点永远处在「没说」这一侧。

    两条断言必须成对：
      ① 危害轴：category 仍是原值（修前必红 —— 先删再插把它盖成了空串）；
      ② 非空断言：content 已经是新内容，证明重索引**真的跑了**。少了 ②，
         「FTS 那半整段短路」也能让 ① 变绿 —— 那是假绿。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import ducky.hot.crud as crud

    mid = "jia14-update-1"
    new_text = "新内容：改喝美式了"
    _jia14_seed(mid, "旧内容：喜欢喝拿铁", _JIA14_CATEGORY)

    class _StubMemory:
        def __init__(self):
            self.updates = []

        def update(self, memory_id, data=None, metadata=None):
            self.updates.append((memory_id, data, dict(metadata or {})))
            return {"message": "ok"}

    mem = _StubMemory()
    monkeypatch.setattr(crud, "get_memory", lambda: mem)

    app = FastAPI()
    crud.register_crud_routes(app)
    client = TestClient(app)

    resp = client.post("/update", json={
        "memory_id": mid, "user_id": _JIA14_USER, "content": new_text,
    })

    problems = []
    if resp.status_code != 200:
        problems.append(f"/update 返回 {resp.status_code}：{resp.text[:200]}")
    if not mem.updates:
        problems.append("/update 压根没走到向量侧写入 —— 本用例证明不了任何事")

    row = _jia14_row(mid)
    if row is None:
        problems.append("更新之后 FTS 行整个不见了")
    else:
        content, category = row
        if content != new_text:
            problems.append(
                f"FTS 行的内容没被重索引（还是 {content!r}）—— 这次更新压根没走到"
                f" _index_memory，下面那条分类断言会假绿"
            )
        if category != _JIA14_CATEGORY:
            problems.append(
                f"一次没提分类的更新把存量分类抹掉了：期望 {_JIA14_CATEGORY!r}，"
                f"实为 {category!r} —— 先删再插时把「没说」当成了「说空」，"
                f"而向量那半靠 payload merge 留住了，两半从此不一样"
            )

    assert not problems, "；".join(problems)


def test_jia14_merge_must_not_erase_the_category_while_it_preserves_the_heat():
    """同一个函数里，上半在保、下半在毁。

    ``_sync_indexes_after_update`` 上面五行专门用 ``preserve_heat=True``
    「保留既有热度」，下面五行原来却硬写 ``category=""``。合并改的是**内容**，
    分类不在合并语义里 —— 保热度和毁分类出现在同一个 try 块的上下两段。

    直接驱动这个函数：它的两个调用方都在 self-edit 合并路径上，要走到那儿得
    先让 LLM 判出一次 merge —— 红灯不该押在一次外部模型调用上。
    """
    from ducky.layer1_selfcheck import _sync_indexes_after_update

    mid = "jia14-merge-1"
    merged = "合并后：项目截止十一月"
    _jia14_seed(mid, "旧内容：项目截止十月", _JIA14_CATEGORY)

    # 第一个形参 memory 在这个函数体里没被用到（只用 memory_id/content/
    # user_id/bank_id），传 None 不是省事，是这个函数真的不碰它。
    _sync_indexes_after_update(None, memory_id=mid, content=merged,
                               user_id=_JIA14_USER)

    problems = []
    row = _jia14_row(mid)
    if row is None:
        problems.append("合并之后 FTS 行整个不见了")
    else:
        content, category = row
        if content != merged:
            problems.append(
                f"内容没被重索引（还是 {content!r}）—— 重索引没跑，下一条会假绿"
            )
        if category != _JIA14_CATEGORY:
            problems.append(
                f"合并顺手把分类抹了：期望 {_JIA14_CATEGORY!r}，实为 {category!r}"
                f" —— 上面刚 preserve_heat=True 保住热度，下面就把分类毁了"
            )

    assert not problems, "；".join(problems)


def test_jia14_rollback_restores_content_without_erasing_the_category():
    """回滚还原的是**内容**，分类从来不在 ``memory_edits`` 的快照里。

    ``rollback_edit`` 原来硬写 ``category=""``：用户每点一次「撤销这次合并」，
    就顺手掉一次分类 —— 撤销这个动作自己造成了一次撤不回来的丢失。
    """
    from ducky.self_edit import _log_edit, rollback_edit

    mid = "jia14-rollback-1"
    restored = "回滚目标：原始内容"
    _jia14_seed(mid, "合并后的内容", _JIA14_CATEGORY)

    edit_id = _log_edit(mid, _JIA14_USER, "merge", restored,
                        "合并后的内容", "用例铺底", 0.9)
    assert edit_id, "编辑账本没写进去，本用例无从谈起"

    class _StubMemory:
        def __init__(self):
            self.updates = []

        def update(self, memory_id, data=None, metadata=None):
            self.updates.append((memory_id, data))
            return {"message": "ok"}

    mem = _StubMemory()
    result = rollback_edit(edit_id, memory=mem)

    problems = []
    if result.get("status") != "ok":
        problems.append(f"回滚没成功：{result!r}")
    row = _jia14_row(mid)
    if row is None:
        problems.append("回滚之后 FTS 行整个不见了")
    else:
        content, category = row
        if content != restored:
            problems.append(
                f"内容没还原（还是 {content!r}）—— 重索引没跑，下一条会假绿"
            )
        if category != _JIA14_CATEGORY:
            problems.append(
                f"一次撤销把分类也撤没了：期望 {_JIA14_CATEGORY!r}，实为 {category!r}"
            )

    assert not problems, "；".join(problems)


def test_jia14_sentinel_keeps_explicit_empty_and_first_index_unchanged():
    """过紧轴：哨兵守的是「分清两种意思」，不是「一律不许写空串」。

    这两条**改前改后都绿** —— 它们守的不是缺陷，是修法的边界：
      ① 显式 ``category=""`` → 照写空串（调用方明确说空，就得听它的）；
      ② 行还不存在时的「没说」→ 落回空串，与修前逐字节一致（回查查不到
         东西，就没有可沿用的分类；首次索引的行为一个字节都没变）。

    少了这两条，把 ``_index_memory`` 改成「空串一律忽略」也能让前三条全绿，
    而那是另一个缺陷：用户从此再也删不掉一个分类。
    """
    from ducky.text_fts import _index_memory, _init_text_fts

    problems = []
    _init_text_fts()

    # ① 说空就写空
    mid = "jia14-explicit-empty-1"
    _jia14_seed(mid, "有分类的存量行", _JIA14_CATEGORY)
    _index_memory(mid, "改成没分类", user_id=_JIA14_USER, category="")
    row = _jia14_row(mid)
    if row is None:
        problems.append("① 显式传空串之后行不见了")
    elif row[1] != "":
        problems.append(
            f"① 调用方明确要求分类为空，却被沿用成了 {row[1]!r} —— 哨兵被写成了"
            f"「空串一律忽略」，那样用户再也删不掉一个分类"
        )

    # ② 没有存量行时，「没说」落回空串（首次索引行为不变）
    fresh = "jia14-never-seen-1"
    assert _jia14_row(fresh) is None, "② 的前置状态不干净：这个 id 本该不存在"
    _index_memory(fresh, "第一次索引", user_id=_JIA14_USER)
    row2 = _jia14_row(fresh)
    if row2 is None:
        problems.append("② 首次索引压根没写进去")
    elif row2[1] != "":
        problems.append(f"② 首次索引的分类应为空串，实为 {row2[1]!r}")

    assert not problems, "；".join(problems)


# ── 甲15：回填时「payload 上没戳 bank」不等于「属于操作者这次点的库」 ──────────
#
# 缺陷在 ``_backfill_text_fts`` 那句 ``bank_id=meta.get("bank_id", bank_id)``：
# 缺省回落到**形参**，也就是「操作者这次想回填哪个库」。可 payload 上没戳 bank
# 的是启用多库之前的存量，按迁移契约它们属于默认库 —— 回落形参等于把一条老记忆
# 永久改判过去。不可逆在于 ``_index_memory`` 的 DELETE 按 (id, user, bank) 三键
# 定位：错戳出来的是**另一行**，不是覆盖，再回填一次也清不掉那行。
#
# ⚠️ 定性：**潜伏，不是活缺陷**。全仓 ``_backfill_text_fts`` 只有一个调用方
# （``text_fts.py`` 里那个冷启动延迟回填线程），它一个作用域参数都不传 → 实参恒
# 为 ``DEFAULT_BANK_ID``，于是今天这句回落与修后**逐字节同义**，线上一条都没错
# 戳过。修它的理由只有一个：留着就是下一个调用方的陷阱（判据同甲13 缺陷②）。
#
# 读侧那半（``get_all`` 既没按 bank 过滤、又只读一个 user）本轮**不碰**，所以下
# 面只把读到的 filters 记下来当射程证明，不对它断言 —— 对一件本轮不修的事发红
# 灯，红的是别人那条轴。
#
# 直读那一行仍借甲14 的 ``_jia14_row``：它本来就按 (id, user, bank) 三键取行，
# 正是这条用例要问的问题，没必要再抄一份。

_JIA15_USER = "jia15_user"
_JIA15_OTHER_BANK = "work"


class _Jia15Memory:
    """只实现 ``get_all`` 的替身：如实返回两条 payload，一条有 bank 戳一条没有。

    不用 MagicMock —— 什么都答应的假库分不清「回填真读到了两条」和「压根没走到
    读这一步」，而这条用例的红灯恰恰要靠这个区分才站得住。
    """

    def __init__(self, items: list):
        self._items = items
        self.filters_seen: list = []

    def get_all(self, filters=None, limit=None):
        self.filters_seen.append(filters)
        return {"results": self._items}


def test_jia15_backfill_must_not_stamp_unbanked_rows_with_the_callers_bank(monkeypatch):
    """回填一批混合存量：一条带 bank 戳、一条不带，而调用方点的是非默认库。

    三条断言各守一轴：
      ① 危害轴：没戳的那条必须落**默认库**（修前必红 —— 它落到了调用方点的库）；
      ② 负向对照：没戳的那条**不在**调用方点的库里（防「两个库各写一份」这种看
         着变绿其实更糟的错修）；
      ③ 过紧轴（修前修后都该绿）：带戳的那条仍落**自己那个戳**，没被一并按默认
         库处理 —— 防「干脆全写 DEFAULT_BANK_ID」这种更严谨的错修。
    """
    from ducky.bank_contract import DEFAULT_BANK_ID
    from ducky.text_fts import _backfill_text_fts, _init_text_fts

    assert _JIA15_OTHER_BANK != DEFAULT_BANK_ID, \
        "用例自身失效：拿来当「非默认库」的名字撞上了默认库"

    _init_text_fts()

    unbanked = "jia15-legacy-nobank-1"
    stamped = "jia15-legacy-stamped-1"
    mem = _Jia15Memory([
        {"id": unbanked, "memory": "老存量：payload 上没有 bank 戳"},
        {"id": stamped, "memory": "新存量：payload 上戳着自己的库",
         "metadata": {"bank_id": _JIA15_OTHER_BANK}},
    ])
    # 靶子是**源模块** ducky.mem0_runtime：产品代码那句 import 写在函数体里，每次
    # 调用才现取名字，打 ducky.text_fts 上的同名属性是打不着的（甲12 的教训）。
    monkeypatch.setattr("ducky.mem0_runtime.get_memory", lambda: mem)

    n = _backfill_text_fts(limit=10, user_id=_JIA15_USER, bank_id=_JIA15_OTHER_BANK)

    # 射程证明放最前：替身真被问过、两条真被灌过。少了这几句，下面三条都可能因为
    # 「整段回填被 except 吞掉」而假绿 —— 那个 except 只 logger.warning 然后
    # return 0，一个字都不抛。
    assert mem.filters_seen, "回填压根没问过 mem0：替身没接上，后面无从谈起"
    assert mem.filters_seen[0].get("user_id") == _JIA15_USER, \
        f"回填读的不是本用例这个租户：{mem.filters_seen[0]!r}"
    assert n == 2, f"两条都该灌进去，实际回填 {n} 条（0 说明整段被 except 吞了）"

    problems = []

    if _jia14_row(unbanked, _JIA15_USER, DEFAULT_BANK_ID) is None:
        problems.append(
            f"① payload 上没戳 bank 的存量没落进默认库：按迁移契约它就属于 "
            f"{DEFAULT_BANK_ID!r}，缺省回落形参等于把它改判到 {_JIA15_OTHER_BANK!r}"
        )

    if _jia14_row(unbanked, _JIA15_USER, _JIA15_OTHER_BANK) is not None:
        problems.append(
            f"② 没戳 bank 的存量出现在调用方点的库 {_JIA15_OTHER_BANK!r} 里 —— 回"
            f"填的归属只能跟源记忆走，不能跟「这次想回填哪个库」走；而且三键定位"
            f"下这是另一行，再回填一次也清不掉"
        )

    if _jia14_row(stamped, _JIA15_USER, _JIA15_OTHER_BANK) is None:
        problems.append(
            f"③ 带戳的那条没落进自己的戳 {_JIA15_OTHER_BANK!r} —— 缺省回落改成硬"
            f"写默认库了？那是把「有戳」和「没戳」又塌成了一件事"
        )

    assert not problems, "；".join(problems)
