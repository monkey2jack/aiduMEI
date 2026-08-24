"""
v20.0 守卫：mem0 运行时补丁层（``ducky/mem0_patches.py``）。

**这条守卫冻结的是「补丁看起来打上了、实际一次都没生效」这一失败形态。**

历史事故（三层，一次挖出来的）：
``ducky/mem0_runtime.py`` 原先的 ``_patch_usage_tracking()`` 把用量追踪打在
``Memory.client`` 上。mem0 的 ``Memory`` 类**没有** ``client`` 属性 —— OpenAI 客户端
挂在 ``Memory.llm.client`` 和 ``Memory.embedding_model.client`` 上。于是：

1. 第一层：``getattr(mem_instance, "client", None)`` 恒为 ``None``，函数在第 7 行
   就 ``return``，后面二十多行是死代码。
2. 第二层：**即使挂载点写对，补丁本身也是坏的。** 原写法
   ``client.chat.completions.create = _tracked.__get__(client, OpenAI)`` 配
   ``_orig_create(self, *args)`` 会多传一个位置参数，抛
   ``TypeError: create() takes 1 argument(s) but 2 were given`` —— 根本到不了网络。
   因为第一层死得早，这一层从未暴露过。**只修挂载点会让生产上每一次 LLM 调用当场崩。**
3. 第三层：``get_memory()`` 无条件打印「用量追踪已激活」。日志说激活了，实际空转 —— 
   这就是铁律 7「宣称即承诺」要治的东西。

同批治的另两个基座缺陷：

* **Role Drop**：``parse_messages()`` 只认 system/user/assistant，其余 role 的
  content 被静默丢弃、零告警。上游 main 至今未修（实测 ``grep -c else`` = 0）。
* **list content 崩**：``remove_code_blocks()`` 吃到多模态 list 形态的 content 会抛
  ``AttributeError: 'list' object has no attribute 'strip'``。此函数在记忆抽取热路径
  （``main.py:973``）上。上游 main 已修但尚未发版。

**为什么断言必须是运行时探针而不是源码 grep：**
基座是第三方包，我们不控制它的源码文本。版本号更靠不住 —— 上游 main 的
``pyproject.toml`` 至今仍写 ``version = "2.0.18"``，装了 main 之后
``pip show mem0ai`` 依然报 2.0.18，任何版本号守卫都会被骗。所以本文件里每一条
断言都是「喂进去、看出来」，不看源码字符串、不看版本号。

**为什么要分别断言两个命名空间：**
``mem0/memory/main.py`` 用的是 ``from mem0.memory.utils import parse_messages`` ——
**按名字绑定**。同一个函数对象在 ``utils`` 和 ``main`` 两个模块各有独立引用，只替换
``utils`` 那份，``main`` 里那份纹丝不动，而生产走的正是 ``main`` 那份。这是猴子补丁
最经典的假绿灯：改完 ``utils`` 一测「补丁生效了」，上线之后一点用没有。
"""

import contextlib
import os

import pytest

PROBE_ROLE = "aidumem_test_role"
PROBE_TEXT = "aidumem_test_payload_zzz"
FENCED = '```json\n{"facts": ["a"]}\n```'


@pytest.fixture(autouse=True)
def _restore_mem0_namespaces():
    """补丁层改的是 mem0 的模块属性（进程全局）。每个用例前后都必须还原，
    否则补丁会漏给同一进程里的其他测试，制造跨文件的假绿灯/假红灯。"""
    import mem0.memory.main as mn
    import mem0.memory.utils as mu

    from ducky import mem0_patches

    saved = [
        (mod, name, getattr(mod, name))
        for mod in (mu, mn)
        for name in ("parse_messages", "remove_code_blocks")
        if hasattr(mod, name)
    ]
    mem0_patches.reset_for_test()
    try:
        yield
    finally:
        for mod, name, fn in saved:
            setattr(mod, name, fn)
        mem0_patches.reset_for_test()


# ═══════════════════════════════════════════════
# 一、基座缺陷现存性（负向对照：补丁前缺陷必须在）
# ═══════════════════════════════════════════════
def test_baseline_role_drop_defect_is_present_before_patching():
    """双向复现的「前」半段：不打补丁时，非标准 role 的 content 必须是丢的。

    这条断言有意会随基座升级而变红 —— 上游哪天修了 Role Drop，它就该红，提醒
    我们去掉本地补丁。红了不是坏事，是到期提醒。
    """
    import mem0.memory.utils as mu

    out = mu.parse_messages([{"role": PROBE_ROLE, "content": PROBE_TEXT}])
    assert PROBE_TEXT not in out, (
        "基座已经不丢非标准 role 了 —— 说明 mem0 升级后修了 Role Drop，"
        "本地 role_drop 补丁可以退役了，请一并更新本用例。"
    )


def test_baseline_list_content_defect_is_present_before_patching():
    """双向复现的「前」半段：不打补丁时，list 形态 content 必须炸。

    同上，这条断言随基座升级而变红即为到期提醒。
    """
    import mem0.memory.utils as mu

    with pytest.raises(AttributeError):
        mu.remove_code_blocks([{"type": "text", "text": PROBE_TEXT}])


# ═══════════════════════════════════════════════
# 二、Role Drop 补丁
# ═══════════════════════════════════════════════
def test_role_drop_patch_rescues_nonstandard_role():
    import mem0.memory.utils as mu

    from ducky.mem0_patches import _patch_role_drop, patch_status

    _patch_role_drop()
    out = mu.parse_messages([{"role": PROBE_ROLE, "content": PROBE_TEXT}])
    assert PROBE_TEXT in out, "补丁打了但 content 还是丢的"
    assert PROBE_ROLE in out, "role 名字应当出现在抽取文本里，否则模型不知道这是谁说的"
    assert patch_status()["counters"]["role_drop_rescued"] >= 1, (
        "救回了内容但计数器没动 —— 计数器是我们唯一能在生产上看见这条路走过的凭据"
    )


def test_role_drop_patch_is_applied_to_both_namespaces():
    """**本文件最重要的一条。**

    ``mem0.memory.main`` 用 ``from ... import parse_messages`` 拿到的是独立引用，
    生产的抽取链路（``main.py:921`` / ``main.py:2576``）走的正是 ``main`` 那份。
    只补 ``utils`` 会得到一个「测试全绿、上线无效」的补丁。
    """
    import mem0.memory.main as mn
    import mem0.memory.utils as mu

    from ducky.mem0_patches import _patch_role_drop, patch_status

    assert mu.parse_messages is mn.parse_messages, (
        "基座导入方式变了（不再是按名字绑定）—— 补丁的重绑策略需要重新评估"
    )
    _patch_role_drop()
    assert PROBE_TEXT in mn.parse_messages([{"role": PROBE_ROLE, "content": PROBE_TEXT}]), (
        "mem0.memory.main 里那份 parse_messages 没被替换 —— 生产走的就是这份"
    )
    ns = patch_status()["patches"]["role_drop"].get("namespaces") or []
    assert "mem0.memory.main" in ns and "mem0.memory.utils" in ns, f"命名空间覆盖不全: {ns}"


def test_role_drop_patch_preserves_standard_roles():
    """负向对照：补丁不许改变三种标准 role 的既有行为，逐字节一致。"""
    import mem0.memory.utils as mu

    from ducky.mem0_patches import _patch_role_drop

    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ]
    before = mu.parse_messages(msgs)
    _patch_role_drop()
    assert mu.parse_messages(msgs) == before == "system: s\nuser: u\nassistant: a\n"


def test_role_drop_patch_keeps_skipping_none_content():
    """上游语义保留：只带 ``tool_calls``、没有 ``content`` 的消息本就该跳过，
    那不是 Role Drop。补丁不许把 ``None`` 也「救」进来变成噪声。"""
    import mem0.memory.utils as mu

    from ducky.mem0_patches import _patch_role_drop

    _patch_role_drop()
    assert mu.parse_messages([{"role": "assistant", "content": None}]) == ""
    assert mu.parse_messages([{"role": PROBE_ROLE, "content": None}]) == ""


# ═══════════════════════════════════════════════
# 三、remove_code_blocks 加固（list 形态 + 空抽取可见化）
# ═══════════════════════════════════════════════
def test_list_content_no_longer_raises():
    import mem0.memory.utils as mu

    from ducky.mem0_patches import _patch_code_block_hardening

    _patch_code_block_hardening()
    assert mu.remove_code_blocks([{"type": "text", "text": PROBE_TEXT}]) == PROBE_TEXT
    assert mu.remove_code_blocks([PROBE_TEXT]) == PROBE_TEXT


def test_code_block_patch_preserves_str_behaviour():
    """负向对照：正常字符串路径打补丁前后必须逐字节一致。

    这条用例本身踩过一次坑：第一版探针在 shell heredoc 里写了 ``'\\\\n'``，Python
    收到的是字面反斜杠+n 而不是换行，正则匹配不上，看起来像「补丁把去围栏搞坏了」。
    是假红灯。所以这里用 ``FENCED`` 常量，换行由 Python 源码保证。
    """
    import mem0.memory.utils as mu

    from ducky.mem0_patches import _patch_code_block_hardening

    before = mu.remove_code_blocks(FENCED)
    assert before == '{"facts": ["a"]}', "基座去围栏行为已变，补丁的前置假设失效"
    _patch_code_block_hardening()
    assert mu.remove_code_blocks(FENCED) == before
    assert mu.remove_code_blocks("<think>x</think>hello") == "hello", "<think> 剥离被破坏"
    assert mu.remove_code_blocks(None) == ""


def test_empty_extraction_is_counted_and_not_silent():
    """A1 现场：LLM 抽取返回空时，原本一片寂静，记忆静静地没写进去。
    补丁不改变返回值（仍是空串，行为兼容），只把这件事变成可见、可计数。"""
    import mem0.memory.utils as mu

    from ducky.mem0_patches import _patch_code_block_hardening, patch_status

    _patch_code_block_hardening()
    assert mu.remove_code_blocks("   ") == "", "空抽取的返回值语义不许变，只许加可观测"
    assert patch_status()["counters"]["empty_extraction"] >= 1, (
        "空抽取发生了但计数器为 0 —— A1 又变回静默了"
    )


def test_empty_extraction_counter_does_not_fire_on_success(caplog):
    """负向对照：正常抽取不许污染 empty_extraction 计数，否则这个指标没法用来
    判断生产上到底空了多少次。"""
    import mem0.memory.utils as mu

    from ducky.mem0_patches import _patch_code_block_hardening, patch_status

    _patch_code_block_hardening()
    mu.remove_code_blocks(FENCED)
    assert patch_status()["counters"]["empty_extraction"] == 0


# ═══════════════════════════════════════════════
# 四、用量追踪：挂载点 + 调用形状
# ═══════════════════════════════════════════════
_PROXY_ENV = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "ALL_PROXY", "all_proxy")


@contextlib.contextmanager
def _without_proxy_env():
    """构造 OpenAI 客户端时把代理环境变量摘掉。

    🔴 这不是洁癖，是一次实测出来的**假红灯**：`OpenAI()` 在构造时就让 httpx 读
    `ALL_PROXY`，而如果那个值是 `socks5h://…` 且本机没装 `socksio`，httpx 直接
    抛 `ImportError: Using SOCKS proxy, but the 'socksio' package is not installed`。
    于是这个文件里 4 条纯桩测试（一次网络都不发）在一台开着 SOCKS 代理的机器上
    集体变红 —— 红得跟真缺陷一模一样，跟被测代码毫无关系。

    假红灯和假绿灯一样害人：它让人开始怀疑没坏的东西，或者更糟 —— 学会无视红灯。
    这些用例的挂载点全部由桩接管，从设计上就不该碰网络，所以这里把代理摘干净，
    让结论只取决于补丁层本身。`test_real_shim_survives_a_socks_proxy_in_the_environment`
    是这条护栏的负向对照。
    """
    saved = {k: os.environ.pop(k, None) for k in _PROXY_ENV}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def _real_shim():
    """用生产真实会用到的类构造挂载点，不用假对象。

    铁律 6：「我断言的这条路，是生产真的会走的那条路吗？」—— 一个 ``MagicMock``
    会让任何 ``getattr`` 都成功，正好把这个缺陷类彻底测不出来。
    """
    from mem0.configs.embeddings.base import BaseEmbedderConfig
    from mem0.configs.llms.base import BaseLlmConfig
    from mem0.embeddings.openai import OpenAIEmbedding
    from mem0.llms.openai import OpenAILLM

    class Shim:
        pass

    s = Shim()
    with _without_proxy_env():
        s.llm = OpenAILLM(BaseLlmConfig(api_key="sk-test-not-real", model="gpt-4o-mini"))
        s.embedding_model = OpenAIEmbedding(
            BaseEmbedderConfig(api_key="sk-test-not-real",
                               model="text-embedding-3-small")
        )
    return s


def test_real_shim_survives_a_socks_proxy_in_the_environment(monkeypatch):
    """负向对照：环境里插一个必然不可用的 SOCKS 代理，挂载点照样构造得出来。

    去掉 `_without_proxy_env()` 这层，这条断言会以 `ImportError` 当场红 ——
    这就是那 4 条假红灯的成因，被单独钉在这里，防止下一个人「顺手清理」掉它。
    端口取 1 是故意的：就算本机装了 `socksio`，那儿也不会有人应答；而这些用例
    压根不发请求，所以「连不上」永远不会影响结论，只有「构造不出来」才会。
    """
    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "socks5h://127.0.0.1:1")
    shim = _real_shim()
    assert shim.llm.client is not None, "LLM 客户端没构造出来"
    assert shim.embedding_model.client is not None, "嵌入客户端没构造出来"
    assert os.environ["ALL_PROXY"] == "socks5h://127.0.0.1:1", (
        "代理变量没还原回去 —— 上下文管理器漏了 finally，会污染同进程后续用例"
    )


def test_memory_class_has_no_client_attribute():
    """把事故根因焊死：``Memory`` 上没有 ``client``。

    这条断言存在的意义是，如果哪天 mem0 真的加了 ``Memory.client``，它会变红，
    提醒我们「当年那个写法现在能跑了」—— 而不是让下一个人重新踩一遍。
    """
    import inspect

    from mem0.memory.main import Memory

    src = inspect.getsource(Memory.__init__)
    assert "self.client" not in src, (
        "mem0 的 Memory 现在有 client 属性了，请重新评估用量追踪的挂载点选择"
    )


def test_usage_tracking_mounts_on_llm_and_embedding_client():
    from ducky.mem0_patches import _patch_usage_tracking, patch_status

    shim = _real_shim()
    assert shim.llm.client is not None and shim.embedding_model.client is not None
    _patch_usage_tracking(shim)
    rec = patch_status()["patches"]["usage_tracking"]
    assert rec["status"] == "applied", f"用量追踪没挂上: {rec}"
    assert set(rec.get("mounted") or []) == {"llm", "embedding_model"}


def test_usage_tracking_wrapper_call_shape_is_correct():
    """**第二层缺陷的守卫。**

    旧写法 ``_orig_create(self, *args)`` 会抛
    ``TypeError: create() takes 1 argument(s) but 2 were given``，在到达网络之前
    就死。这里把网络那一跳换成桩：桩被成功调用，就证明包装层的参数形状是对的
    （旧形状根本到不了桩）。
    """
    from ducky.mem0_patches import _patch_usage_tracking, patch_status

    class FakeUsage:
        prompt_tokens, completion_tokens, total_tokens = 11, 22, 33

    class FakeResp:
        usage = FakeUsage()

    calls = []
    shim = _real_shim()

    # 桩必须是 keyword-only —— 真实的 openai `create` 实测只接受 `self` 一个位置参数，
    # 取到的绑定方法则一个位置参数都不收。用 `lambda *a, **k` 当桩会把多传的 self
    # 静静吃掉，这条守卫就守不住第二层缺陷了。
    def _stub_llm(*, model, messages, **k):
        calls.append(("llm", model))
        return FakeResp()

    def _stub_embed(*, model, input, **k):
        calls.append(("embed", model))
        return FakeResp()

    shim.llm.client.chat.completions.create = _stub_llm
    shim.embedding_model.client.embeddings.create = _stub_embed

    _patch_usage_tracking(shim)
    # 若形状写错，下面两行会抛 TypeError 而不是返回 FakeResp
    shim.llm.client.chat.completions.create(model="x", messages=[])
    shim.embedding_model.client.embeddings.create(model="x", input="hi")

    assert len(calls) == 2, "包装层没把调用透传到底层 client"
    c = patch_status()["counters"]
    assert c["llm_calls_tracked"] == 1 and c["embed_calls_tracked"] == 1, (
        f"调用透传了但用量没记上: {c}"
    )


def test_usage_tracking_passes_no_positional_args():
    """**第二层缺陷的贴身守卫。**

    实测：``openai`` 的 ``create`` 只接受 ``self`` 一个位置参数，取到的绑定方法则
    一个都不收。老写法 ``_orig_create(self, *args)`` 多传了一个 ``self``，必抛
    ``TypeError``。这里的桩**完全不接受位置参数** —— 包装层一旦多传，立刻炸。
    """
    from ducky.mem0_patches import _patch_usage_tracking

    class FakeUsage:
        prompt_tokens, completion_tokens, total_tokens = 1, 1, 2

    class FakeResp:
        usage = FakeUsage()

    seen = {}

    def strict(**kwargs):
        seen.update(kwargs)
        return FakeResp()

    shim = _real_shim()
    shim.llm.client.chat.completions.create = strict
    _patch_usage_tracking(shim)
    shim.llm.client.chat.completions.create(model="m", messages=[{"role": "user", "content": "x"}])
    assert seen.get("model") == "m", "kwargs 没原样透传到底层 client"


def test_usage_tracking_is_idempotent():
    """反复 ``apply`` 不许套娃：套两层会让每次调用被记两遍账，用量指标翻倍。"""
    from ducky.mem0_patches import _patch_usage_tracking, patch_status

    class FakeUsage:
        prompt_tokens, completion_tokens, total_tokens = 1, 1, 2

    class FakeResp:
        usage = FakeUsage()

    shim = _real_shim()
    shim.llm.client.chat.completions.create = lambda **k: FakeResp()
    shim.embedding_model.client.embeddings.create = lambda **k: FakeResp()
    _patch_usage_tracking(shim)
    _patch_usage_tracking(shim)
    _patch_usage_tracking(shim)
    shim.llm.client.chat.completions.create(model="x", messages=[])
    assert patch_status()["counters"]["llm_calls_tracked"] == 1, "补丁套娃了，用量被重复计账"


def test_usage_tracking_reports_drift_when_mount_point_missing():
    """铁律 8：挂不上的时候必须**说**挂不上，不许静默 return。

    这正是老代码的死法 —— 它 ``return`` 得干干净净，然后由上层打印「已激活」。
    """
    from ducky.mem0_patches import _patch_usage_tracking, patch_status

    class Hollow:
        pass

    _patch_usage_tracking(Hollow())
    rec = patch_status()["patches"]["usage_tracking"]
    assert rec["status"] == "drift", f"挂载点全无却没报 drift: {rec}"
    assert patch_status()["ok"] is False, "有 drift 时 /health 必须为不 ok"
    assert rec.get("missing"), "报了 drift 但没说缺什么，运维无从下手"


# ═══════════════════════════════════════════════
# 五、铁律 7：日志不许无条件宣称
# ═══════════════════════════════════════════════
def test_no_unconditional_activation_claim_in_runtime():
    """``get_memory()`` 不许再无条件打印「用量追踪已激活」。

    这条是源码级断言 —— 因为要守的正是「一句写死的话」，运行时探针看不见它是不是
    写死的。其余每一条断言都是运行时的。
    """
    import pathlib

    src = pathlib.Path("ducky/mem0_runtime.py").read_text(encoding="utf-8")
    offenders = [
        ln.strip()
        for ln in src.splitlines()
        if "用量追踪已激活" in ln and "logger." in ln
    ]
    assert not offenders, f"又出现无条件的激活宣称: {offenders}"
    assert "from ducky.mem0_patches import apply_all" in src, (
        "mem0_runtime 没有委托到补丁层 —— 补丁层可能被绕过了"
    )


def test_health_exposes_patch_ledger():
    """补丁台账必须能被 /health 读到，否则生产上没人知道补丁是不是掉了。"""
    from ducky.mem0_runtime import mem0_patch_status

    st = mem0_patch_status()
    for key in ("ok", "problems", "patches", "counters"):
        assert key in st, f"补丁台账缺字段 {key}"
