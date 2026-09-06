"""P3-8 降权的守卫（v20.0）。

这一批守的是**发货物**里的身份与能力面：systemd 单元模板、Dockerfile、
docker-compose。它们不像代码那样有测试兜底，改坏了也不会有人当场发现 ——
坏法是「装的人以 root 跑了三年，谁也没注意」。

三条纪律写在前面，后面每条用例都按它来：

① **不靠 grep 判断「有没有配」**。注释里出现 `User=root` 和真的配了
   `User=root` 是两件完全不同的事。本文件一律走 unit 的段落解析器
   （复用 test_v19_4_2_auth_coverage 里那个），只看有效指令行。

② **每条都要有射程**。只断言「现在是对的」的用例，在文件被整体删空时
   也会绿。所以每条都同时要正面锚点（该有的确实有）和负面断言
   （不该有的确实没有）。

③ **配置写了不等于配置生效**。本文件只能证明模板写对了；生效值必须在机器上
   用 `systemctl show` / `systemd-analyze security` 验，两者不能互相替代。
   这一点在 v19.4.2 的 StartLimit* 事故上已经付过学费。
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

import pytest
import yaml   # 硬依赖（requirements-dev.txt / pyproject 的 dev 组都声明了）。
             # 刻意**不用** importorskip：那会凭空多出一条跳过轴，而
             # tests/test_v20_skip_axis_census.py 会当场把它记到账上，
             # README 的「全轴齐备」前提也要跟着改。仓里既有的
             # tests/test_v20_ci_pipeline.py 就是直接 import，此处同构。

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _unit_sections(path: pathlib.Path) -> dict:
    """把一个 systemd unit 拆成 {段名: [有效指令行]}。

    与 test_v19_4_2_auth_coverage 里同名函数同源：注释和空行必须丢掉，
    否则本文件那些大段「为什么这么配」的注释会被误当成配置读进来 ——
    而本轮注释里恰好反复出现 `User=root`、`ProtectHome=yes` 这些串。
    """
    sections, current = {}, None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def _directives(path: pathlib.Path, section: str = "Service") -> dict:
    """{键: 最后一次赋值}。systemd 对多数键取最后一次出现的值，这里同构。"""
    out = {}
    for line in _unit_sections(path).get(section, []):
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


_DEPLOY = _REPO_ROOT / "deploy"
_API_UNIT = _DEPLOY / "aidumem-api.service"
_SYNC_UNIT = _DEPLOY / "aidumem-sync.service"
_DOCKERFILE = _REPO_ROOT / "Dockerfile"
_COMPOSE = _REPO_ROOT / "docker-compose.yml"


# ══════════════════════════════════════════════════
# systemd 单元
# ══════════════════════════════════════════════════

def test_no_unit_template_runs_as_root():
    """任何 deploy/*.service 都不许配 User=root（P3-8 的正题）。

    v20.0 之前两个模板都写着 User=root。一个只在回环上读写记忆的服务，
    拿着 CAP_SYS_ADMIN / CAP_SYS_PTRACE 这类能力没有任何用途，
    只是把一次依赖链上的 RCE 从「丢记忆」放大成「丢整机」。

    ★ 按段扫描仓库里**每一个** unit 模板，新增单元自动纳入射程。
    ★ 允许 `User=` 缺省（不写）—— aidumem-sync 刻意保留了这个自由度：
      它要读的 MEMORY.md 常常是某个真人家目录里的 600 文件，换 uid 就得
      放宽那个文件的权限。用「私有笔记可读面变宽」换「守护不是 root」
      多数情况不划算，所以那个模板只加固能力面，uid 交给部署方决定。
      缺省与显式 root 的差别是实质性的：前者是「你自己选」，后者是
      「我们发的默认就是最高权限」。
    """
    units = sorted(_DEPLOY.glob("*.service"))
    assert units, "deploy/ 下一个 .service 模板都没有 —— 守卫失去着力点（可能被移动了）"

    offenders = []
    for path in units:
        user = _directives(path).get("User")
        if user is not None and user.strip() == "root":
            offenders.append(path.name)
    assert not offenders, (
        "以下单元模板仍以 root 运行：" + ", ".join(offenders)
        + "\n要么改成专用系统账号，要么整行去掉让部署方自己决定 —— "
        "但不许发一个默认最高权限的模板。"
    )


def test_api_unit_names_a_dedicated_account_and_group():
    """API 单元必须显式指定专用账号**和**组（正面锚点）。

    上一条只证明「没写 root」，把 User= 整行删掉它也绿。API 这边没有
    sync 那种家目录读取的顾虑（它一个家目录都不碰），所以必须坐实身份。

    Group= 是单独一条：只写 User= 时 systemd 用该账号的主组，通常等于同名组，
    但在部署方复用已有账号时不一定 —— 而数据目录是按组交接的。
    """
    d = _directives(_API_UNIT)
    user = d.get("User")
    assert user and user != "root", (
        f"aidumem-api.service 的 User= 是 {user!r}。API 不读任何家目录，"
        "没有理由不坐实一个专用账号。"
    )
    assert d.get("Group"), (
        "只写了 User= 没写 Group=。systemd 会取该账号的主组，"
        "而数据目录是按组 chown 交接的 —— 部署方复用已有账号时主组未必同名，"
        "表现是「读得到、写不进」。显式写出来。"
    )


@pytest.mark.parametrize("unit", [_API_UNIT, _SYNC_UNIT], ids=lambda p: p.name)
def test_unit_drops_all_capabilities_and_blocks_privilege_gain(unit: pathlib.Path):
    """两个单元都必须清空 capability 并禁止提权。

    这一条对 aidumem-sync 尤其重要：那个模板的 User= 是允许缺省的
    （可能以 root 跑）。一个 capability 全清、文件系统只读、系统调用受限的
    root，攻击面已远小于裸 root —— 所以能力面加固是它唯一的护栏，
    不能因为「反正可能是 root」就省掉。
    """
    d = _directives(unit)
    assert d.get("NoNewPrivileges") == "yes", (
        f"{unit.name} 缺 NoNewPrivileges=yes。生产实测两个单元此前都是 "
        "NoNewPrivileges=no（systemctl show 可验），setuid 二进制仍能提权。"
    )
    assert d.get("CapabilityBoundingSet") == "", (
        f"{unit.name} 的 CapabilityBoundingSet 不是空。"
        "留空才是「一个都不给」；服务监听 8767（>1024），"
        "连 CAP_NET_BIND_SERVICE 都不需要。"
    )
    assert d.get("RestrictSUIDSGID") == "yes", f"{unit.name} 缺 RestrictSUIDSGID=yes"


@pytest.mark.parametrize("unit", [_API_UNIT, _SYNC_UNIT], ids=lambda p: p.name)
def test_unit_mounts_filesystem_readonly_with_explicit_write_holes(unit: pathlib.Path):
    """ProtectSystem=strict 必须与 ReadWritePaths 成对出现。

    单有 strict 而没开洞 = 服务起来就写不进，第一次写库才炸；
    单有 ReadWritePaths 而没有 strict = 那行等于装饰。两者必须同时在。

    ★ 这条也顺手守住一个易漏点：日志目录也必须在 ReadWritePaths 里。
      StandardOutput=append: 由 systemd 打开，落在只读挂载上时
      单元直接 failed，日志里只有一句 Read-only file system ——
      极易误判成磁盘故障。
    """
    d = _directives(unit)
    assert d.get("ProtectSystem") == "strict", (
        f"{unit.name} 的 ProtectSystem 不是 strict（当前 {d.get('ProtectSystem')!r}）"
    )
    rw = d.get("ReadWritePaths", "")
    assert rw, f"{unit.name} 有 ProtectSystem=strict 却没有 ReadWritePaths —— 服务写不进任何东西"

    log_target = None
    for key in ("StandardOutput", "StandardError"):
        val = d.get(key, "")
        if val.startswith("append:"):
            log_target = val.split(":", 1)[1]
            break
    if log_target:
        log_dir = os.path.dirname(log_target)
        assert any(log_dir.startswith(p) or p.startswith(log_dir) for p in rw.split()), (
            f"{unit.name} 把日志写到 {log_dir}，但它不在 ReadWritePaths（{rw}）里。"
            "systemd 打开 append: 目标时会因只读挂载失败，单元直接 failed。"
        )


def test_sync_unit_does_not_hide_the_home_dir_it_must_read():
    """aidumem-sync 不许配 ProtectHome —— 它要读的就是家目录里的文件。

    这是一条**反向**守卫：防的是「照抄 API 单元」这个非常自然的动作。
    mem0_sync.py 默认读 ~/.hermes/memories/MEMORY.md。加上 ProtectHome=yes
    之后的表现是 FileNotFoundError —— 看着像路径配错，实际是被沙箱藏了。

    对照组：API 单元**应该**有 ProtectHome=yes（它一个家目录都不读）。
    两条一起断言，这条才有区分力 —— 否则「两个单元都没有」也能让它绿。
    """
    sync_d = _directives(_SYNC_UNIT)
    assert "ProtectHome" not in sync_d, (
        f"aidumem-sync.service 配了 ProtectHome={sync_d.get('ProtectHome')!r}。"
        "该守护要读 ~/.hermes/memories/MEMORY.md，家目录被藏起来后会报 "
        "FileNotFoundError —— 像路径配错，其实是沙箱。"
    )
    api_d = _directives(_API_UNIT)
    assert api_d.get("ProtectHome") == "yes", (
        "aidumem-api.service 缺 ProtectHome=yes。API 不读任何家目录，"
        "这道墙是免费的。（本断言同时给上一句提供区分力：不能两个单元都没配。）"
    )


@pytest.mark.parametrize("unit", [_API_UNIT, _SYNC_UNIT], ids=lambda p: p.name)
def test_unit_keeps_proc_self_readable_for_the_health_probe(unit: pathlib.Path):
    """不许配 ProcSubset=pid —— 会削掉 /health 的资源探针。

    ducky/resource_probe.py 读 /proc/self/status 取 RSS，那是 /health 上
    唯一的内存指标，而 /health 是事故当时唯一还能问的东西。
    ProcSubset=pid 在部分内核上会让探针退化成 None ——
    宁可少一分硬化，也不要让唯一的可观测入口变哑。

    这条是**反向**守卫：拦的是「把 systemd-analyze 分数刷满」这个冲动。
    """
    d = _directives(unit)
    assert "ProcSubset" not in d, (
        f"{unit.name} 配了 ProcSubset={d.get('ProcSubset')!r}。"
        "这会让 /health 的 rss_mb 探针在部分内核上失效 —— "
        "拿可观测性换一分暴露分，不划算。"
    )
    assert d.get("ProtectProc") == "invisible", (
        f"{unit.name} 缺 ProtectProc=invisible —— 看不见别人的进程，"
        "但自己的 /proc/self 仍可读，这是既硬化又不瞎的那个点。"
    )


def test_unit_syscall_filter_is_not_narrower_than_system_service():
    """SystemCallFilter 必须恰好是 @system-service，不许收得更窄。

    resource_probe 在 /proc 读不到 fd 数时会退到 subprocess lsof。
    比 @system-service 更窄的过滤器会让这条退路以 EPERM 失败 ——
    它有 try/except 兜底不至于崩，但 open_fds 这个指标会永久丢掉。
    """
    for unit in (_API_UNIT, _SYNC_UNIT):
        d = _directives(unit)
        val = d.get("SystemCallFilter")
        assert val == "@system-service", (
            f"{unit.name} 的 SystemCallFilter 是 {val!r}，期望恰好 @system-service。"
            "更窄会掉指标，更宽等于没过滤。"
        )
        assert d.get("SystemCallArchitectures") == "native", (
            f"{unit.name} 缺 SystemCallArchitectures=native —— "
            "不然 32 位兼容层可以绕开上面那道过滤器。"
        )


def test_api_unit_documents_the_data_dir_handover_before_demotion():
    """降权模板必须把「先交接数据目录」写在文件里（顺序错 = 起不来）。

    这条守的不是配置而是**文档**，因为 P3-8 的真实事故形态是顺序问题：
    先改 User= 再 chown，服务在这中间起不来；而 SQLite 的坏法更隐蔽 ——
    只 chown 主库文件、目录仍归 root，只读查询全绿，第一次写才炸。
    模板里必须写清 chown 的是**整棵目录**。

    只要求关键词共现，不锚死措辞（否则重写注释就会误报）。
    """
    text = _API_UNIT.read_text(encoding="utf-8")
    for token in ("useradd", "chown -R", "data", "logs"):
        assert token in text, (
            f"aidumem-api.service 的注释里没有 {token!r} —— "
            "降权前置步骤必须写在模板里，装的人不会去翻 CHANGELOG。"
        )
    assert re.search(r"-wal|WAL", text), (
        "模板没提 WAL。SQLite 写事务要在**同目录**建 -wal/-shm，"
        "所以必须 chown 整棵目录而不是主库文件 —— 这是最容易漏的一条，要写出来。"
    )
    assert "cron" in text, (
        "模板没提 cron。ducky.utils 在 import 时就 ensure_evolution_tables() 建连，"
        "所以哪怕是纯 HTTP 客户端的脚本（consolidator.py）只要 import 了它，"
        "就会以当前身份打开 facts.db 并可能建出 root 属主的 -wal —— "
        "留一个 root 写手就会周期性把整棵目录重新污染成混属主。"
    )


# ══════════════════════════════════════════════════
# 容器
# ══════════════════════════════════════════════════

def test_dockerfile_switches_to_a_non_root_user():
    """Dockerfile 必须有 USER，且不是 root。

    容器里的 root 不是「隔离的 root」—— 它和宿主机 uid 0 是同一个 uid。
    v20.0 之前本镜像没有 USER 行，等于以宿主机 root 跑。
    """
    lines = [ln.strip() for ln in _DOCKERFILE.read_text(encoding="utf-8").splitlines()]
    users = [ln.split(None, 1)[1].strip() for ln in lines
             if ln.upper().startswith("USER ")]
    assert users, "Dockerfile 没有 USER 指令 —— 容器以 root 运行"
    final = users[-1]
    assert final not in ("root", "0", "0:0"), f"Dockerfile 最终 USER 是 {final!r}"


def test_dockerfile_pins_a_fixed_uid_matching_the_compose_mount_note():
    """容器内 uid 必须写死，且与 compose 里 chown 的号一致。

    bind mount 不做 uid 映射：容器里的写入以容器内 uid 落到宿主机文件上。
    让 useradd 自选 uid 的话，镜像重建后可能换号，宿主机上那批 data/
    就突然写不进了 —— 症状是「读得到、写不进」，不是启动失败，
    几乎不可能联想到 uid。

    本条把 Dockerfile 与 docker-compose.yml 的数字**对齐**检查：
    两处各自写死但写成不同的号，比都不写死更坏。
    """
    df = _DOCKERFILE.read_text(encoding="utf-8")
    uids = set(re.findall(r"--uid[= ](\d+)", df))
    assert uids, "Dockerfile 里 useradd 没有 --uid，uid 由系统自选 —— 镜像重建可能换号"
    assert len(uids) == 1, f"Dockerfile 里出现多个 --uid：{sorted(uids)}"
    uid = uids.pop()

    compose = _COMPOSE.read_text(encoding="utf-8")
    assert re.search(rf"chown[^\n]*\b{uid}\b", compose), (
        f"Dockerfile 用 uid={uid}，但 docker-compose.yml 的注释里没有对应的 "
        f"chown ... {uid} 指引。挂载目录属主对不上时服务能起来但写不进。"
    )


def test_dockerfile_hands_over_only_the_writable_dirs():
    """只 chown data/ 与 logs/，不许 chown 整个 /app。

    代码目录保持 root 属主、进程只读，等于免费拿到「运行期改不了自己代码」。
    代价只是 __pycache__ 写不进（Python 静默降级，不报错）。
    """
    df = _DOCKERFILE.read_text(encoding="utf-8")
    bad = [ln.strip() for ln in df.splitlines()
           if not ln.strip().startswith("#")
           and re.search(r"chown\s+(-\S+\s+)*\S+\s+/app\s*$", ln.strip())]
    assert not bad, (
        "Dockerfile 把整个 /app 交给了运行账号：" + "; ".join(bad)
        + "\n代码目录应保持 root 属主只读 —— 运行期改不了自己代码是免费的收益。"
    )
    assert re.search(r"chown[^\n]*/app/data", df), "Dockerfile 没有把 /app/data 交给运行账号"
    assert re.search(r"chown[^\n]*/app/logs", df), "Dockerfile 没有把 /app/logs 交给运行账号"

    # 交出可写目录还不够 —— $HOME 得**指到**其中一个。
    # 这一条是 test_api_unit_gives_the_service_a_writable_home 的容器侧对偶：
    # 那边生产实测踩过（mem0 SDK import 期写 $HOME，缺了就带着绿灯失能），
    # systemd 一侧修了并立了守卫，Dockerfile 一侧同样的洞没人看着。
    home = re.findall(r'^\s*ENV\s+HOME=[\"\']?(\S+?)[\"\']?\s*$', df, re.M)
    assert home, (
        "Dockerfile 没有 ENV HOME —— useradd --no-create-home 之后 $HOME 指向"
        "不存在的目录，mem0 SDK 在 import 期写它，失败后服务照常起、/health 照常绿，"
        "只有向量检索静默零召回。"
    )
    assert home[-1] in ("/app/data", "/app/logs"), (
        f"ENV HOME={home[-1]} 不是交给运行账号的那两个目录之一 —— "
        "指到只读目录等于没设。"
    )


def test_compose_drops_capabilities_and_blocks_privilege_gain():
    """compose 侧再补一道：cap_drop ALL + no-new-privileges。

    镜像里已经是非 root；这两行防的是「镜像被换成以 root 起的版本」。
    走 YAML 解析而不是 grep —— 注释里写着的和真配上的是两件事。
    """
    with _COMPOSE.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    svc = doc["services"]["aidumem"]
    assert [str(x).upper() for x in svc.get("cap_drop", [])] == ["ALL"], (
        f"docker-compose.yml 的 cap_drop 是 {svc.get('cap_drop')!r}，期望 [ALL]"
    )
    opts = [str(x).replace(" ", "") for x in svc.get("security_opt", [])]
    assert "no-new-privileges:true" in opts, (
        f"docker-compose.yml 的 security_opt 是 {svc.get('security_opt')!r}，"
        "缺 no-new-privileges:true"
    )
    assert not svc.get("privileged"), "docker-compose.yml 出现了 privileged —— 这会把上面两条全部作废"


# ══════════════════════════════════════════════════
# 生产实测打回来的三条（v20.0 P3-8 上线当天）
#
# 这三条的来路和上面不一样：上面那些是「设计时想到的」，这三条是**在生产机身上
# 真的踩了**才补的。第一版模板通过了上面全部 15 条守卫，然后在生产上
# ① 让向量库带着绿灯静默失能、② 让同步守护启动即崩溃循环、
# ③ 撞上一个「root 写不进自己机器上的目录」的反直觉权限问题。
#
# 守卫的射程小于缺陷的分布 —— 又一次。所以补在这里。
# ══════════════════════════════════════════════════

def test_api_unit_gives_the_service_a_writable_home():
    """API 单元必须配 StateDirectory + HOME（生产实测：缺了会带着绿灯失能）。

    `useradd --no-create-home` 之后 `$HOME` 指向一个不存在的目录，而 mem0 SDK
    在 import 期要往 `$HOME` 下写缓存。缺这两行在生产上的真实表现是：

        systemctl is-active  → active          （服务确实活着）
        curl /health         → status=ok       （健康检查是绿的）
        但 degraded=['vector_backend']，向量检索静默零召回
        journal 里只有一行 `mem0 SDK 加载失败: [Errno 13]
        Permission denied: '/home/aidumem'`

    **不是崩溃，是带着绿灯失能** —— 这比崩溃难发现得多，因为按 failed
    告警的监控一辈子等不到。这条守卫的存在就是为了不让人把这两行删掉。

    刻意断言 `StateDirectory` 而不只是 `HOME`：手写一个 `Environment=HOME=`
    指向的目录不会被自动创建、也不会自动进 ReadWritePaths，
    在 `ProtectSystem=strict` 之下照样写不进。StateDirectory 才是完整答案。
    """
    d = _directives(_API_UNIT)
    assert d.get("StateDirectory"), (
        "aidumem-api.service 缺 StateDirectory —— mem0 SDK 需要可写 $HOME。"
        "缺了不会崩，只会让 vector_backend 静默失能而 /health 仍报 ok。"
    )
    home = d.get("Environment", "")
    envs = [v for k, v in _directives(_API_UNIT).items()]
    raw_env = [ln for ln in _unit_sections(_API_UNIT).get("Service", [])
               if ln.startswith("Environment=")]
    assert any("HOME=" in ln for ln in raw_env), (
        "aidumem-api.service 配了 StateDirectory 但没有 Environment=HOME= —— "
        "systemd 不会自动把 $HOME 指过去，SDK 还是会去写 /home/<账号>。"
    )
    state = d["StateDirectory"].strip()
    home_line = next(ln for ln in raw_env if "HOME=" in ln)
    assert state in home_line, (
        f"StateDirectory={state} 与 {home_line} 指的不是同一个地方 —— "
        "HOME 指向的目录必须就是 systemd 替你创建并 chown 的那个，"
        "否则 ProtectSystem=strict 之下依然写不进。"
    )


def test_sync_unit_can_write_the_data_dir_it_opens_at_import_time():
    """sync 的 ReadWritePaths 必须含 data/（生产实测：缺了启动即崩溃循环）。

    这条守的是一个非常容易被「讲道理」讲掉的结论：mem0_sync.py 确实**不写
    任何数据库** —— 它只读 MEMORY.md 再 POST。所以第一版模板只给了 logs/。
    结果启动即 `sqlite3.OperationalError: unable to open database file`。

    完整 traceback 追下来的链条是：

        mem0_sync.py  from ducky.utils import DEFAULT_USER_ID, api_auth_headers
          → ducky/__init__.py      from .recall_funnel import funnel_search
          → recall_funnel.py → scoring.py
          → ducky/salience/__init__.py  _ensure_db()   ← 模块级，无 try/except
          → 打开 data/salience.db

    **import 两个常量会拽进整条召回栈。** 「这个进程不写库」是对业务逻辑的
    正确描述，却是对进程行为的错误描述 —— 而沙箱管的是后者。

    ★ 断言 data/ 而不是断言「和 API 一样」：两个单元的可写集合本就不同
      （sync 还要写 ~/.hermes 下的状态文件），锚死相等会误报。
    """
    rw = _directives(_SYNC_UNIT).get("ReadWritePaths", "")
    assert rw, "aidumem-sync.service 没有 ReadWritePaths"
    assert any(seg.rstrip("/").endswith("/data") for seg in rw.split()), (
        f"aidumem-sync.service 的 ReadWritePaths（{rw}）里没有 data/。"
        "ducky/salience/__init__.py 在 import 期就 _ensure_db() 打开 "
        "data/salience.db（模块级调用，没有 try/except），"
        "所以哪怕本守护业务上不写库，进程也必须能写 data/ —— "
        "否则启动即 unable to open database file，按 StartLimit 崩溃循环。"
    )


@pytest.mark.parametrize("unit", [_API_UNIT, _SYNC_UNIT], ids=lambda p: p.name)
def test_unit_creates_group_writable_files_for_the_shared_data_dir(unit: pathlib.Path):
    """两个单元都要 UMask=0007 —— data/ 是两个身份共写的目录。

    API 以专用账号跑、sync 可能仍以 root 跑（它为什么不换 uid 见那个模板），
    但它们写的是**同一批 SQLite 库**。默认 umask 022 会建出 644 的文件，
    另一方就写不进 —— 而症状是「读得到、写不进」，不是启动失败：
    只读查询全部正常、/health 也绿，直到第一次写入才炸。

    UMask 必须与目录的 setgid 位配对才完整（模板头里写了 chmod 2770）：
      · 只有 setgid，没有 UMask → 新文件 640，组不可写
      · 只有 UMask，没有 setgid → 新文件属组是创建者主组，组根本对不上
    两者缺一个都会重演混属主。这条只能守住配置里的那一半，
    另一半（setgid）只能靠文件头的安装说明 —— 所以下一条守卫盯着那段说明。
    """
    umask = _directives(unit).get("UMask")
    assert umask == "0007", (
        f"{unit.name} 的 UMask 是 {umask!r}，期望 0007。"
        "data/ 由 API 与 sync 两个身份共写，默认 022 建出的 644 文件对方写不进。"
    )


def test_templates_document_the_setgid_and_dac_override_traps():
    """安装说明必须写清 setgid，以及「无能力 root 写不进」这件反直觉的事。

    这两条都不是配置能表达的，只能靠文档，而它们各自都足以让部署失败：

    ① **setgid**：模板头必须写 `chmod 2770` 而不是 750。少了开头那个 2，
       新文件不继承目录组，UMask 那半就白配了。

    ② **CAP_DAC_OVERRIDE**：sync 模板必须解释清楚 —— root 之所以能无视
       权限位靠的就是这个 capability，而我们把 CapabilityBoundingSet 清空了。
       于是「root 在自己机器上写不进一个目录」。不写下来的话，下一个人
       百分之百会把 SupplementaryGroups 当成冗余删掉，然后重现崩溃循环。

    只要求关键词共现，不锚死措辞 —— 否则重写注释就误报。
    """
    api = _API_UNIT.read_text(encoding="utf-8")
    assert "2770" in api, (
        "aidumem-api.service 的安装说明没写 chmod 2770 —— "
        "少了 setgid，UMask=0007 那一半就失效，混属主会重演。"
    )

    sync = _SYNC_UNIT.read_text(encoding="utf-8")
    assert "CAP_DAC_OVERRIDE" in sync, (
        "aidumem-sync.service 没有解释 CAP_DAC_OVERRIDE。"
        "清空 capability 之后 root 会失去无视权限位的能力，"
        "这是 SupplementaryGroups 存在的唯一理由 —— "
        "不写下来，下一个人一定会把它当冗余删掉。"
    )
    assert "SupplementaryGroups" in sync, "aidumem-sync.service 缺 SupplementaryGroups"
