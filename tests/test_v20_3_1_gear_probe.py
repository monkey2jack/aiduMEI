"""v20.3.1（九份审计 P0-2）：半开主动探针必须真的会重挂。

上一版的病：`_PROBE_TIMER` 拿着已触发完毕的 Timer 对象永不清空，
`_schedule_probe_timer` 的 `is not None` 门卫恒命中 → 整个进程生命周期里
主动探测只在启动后跑一次就永久停摆。Release Notes 宣称的
"no longer traffic-dependent" 在启动 30 秒后作废。

本文件全部是**行为测试**：注入短 interval + 计数替身，断言探针在多个
间隔内被真实调用多次。旧版那种「断言源码里有 `ensure_half_open_probe_daemon`
这个字符串」的用例（test_first_run_experience.py:476 附近）抓不住这个病——
符号在 ≠ 机制跑。判据落在进程行为上，不落在源码文本上（Qwen 方法论）。
"""

import threading
import time

import pytest

import ducky.gear as gear


@pytest.fixture()
def probe_rig(monkeypatch, tmp_path):
    """可控的探针环境：短 interval + 假 cloud 腿 + 计数器。

    替身只暴露生产真实的 API 面：
    - get_memory() → object with embedding_model.embed(text, kind)
    - call_llm(prompt, ...) → Optional[str]
    （替身签名必须与生产对齐 —— v20.2.4 的教训：替身比生产宽松，
     缺陷就隐形。）
    """
    calls = {"embed": 0, "llm": 0}

    class _EmbedClient:
        @staticmethod
        def embed(text, kind):          # 生产真实签名: embed(query, kind)
            calls["embed"] += 1
            return [0.1, 0.2]

    class _Memory:
        embedding_model = _EmbedClient()

    monkeypatch.setattr(gear, "_PROBE_INTERVAL", 0.05, raising=False)
    # 强制两条腿都处于 lite + half-open 可探测态
    monkeypatch.setattr(gear, "should_try_cloud", lambda **kw: True)
    monkeypatch.setattr(gear, "should_try_llm", lambda **kw: True)
    monkeypatch.setattr(gear._EMBED, "mode", lambda **kw: "lite", raising=False)
    monkeypatch.setattr(gear._LLM, "mode", lambda **kw: "lite", raising=False)
    # 替身对齐生产：get_memory 在 ducky.mem0_runtime（探针函数体内 import）
    import ducky.mem0_runtime as mr
    monkeypatch.setattr(mr, "get_memory", lambda: _Memory(), raising=False)
    # call_llm 在 ducky.llm_client（探针函数体内 import）。
    # 第一版替身忘了带计数副作用——calls['llm'] 恒 0，探针实际跑了 4 次
    # （llm_success 证明），fixture 自己造了个假 0。教训同款：判据要看世界。
    import ducky.llm_client as lc
    def _fake_call_llm(prompt, **kw):
        calls["llm"] += 1
        return "ok"
    monkeypatch.setattr(lc, "call_llm", _fake_call_llm, raising=False)
    # 记录信号上报（不改计数语义，只观察）
    recorded = {"embed_success": 0, "llm_success": 0}
    monkeypatch.setattr(gear, "record_cloud_success",
                        lambda **kw: recorded.__setitem__("embed_success", recorded["embed_success"] + 1))
    monkeypatch.setattr(gear, "record_llm_success",
                        lambda **kw: recorded.__setitem__("llm_success", recorded["llm_success"] + 1))
    yield calls, recorded
    # 清理：终止残留定时器，避免守护线程跨用例
    timer = gear._PROBE_TIMER
    if timer is not None:
        timer.cancel()
    gear._PROBE_TIMER = None


def test_probe_rearms_after_first_fire(probe_rig):
    """核心判据：第一次触发之后，探针必须再次被调度（≥3 次）。

    旧版在这里必然失败：第一次 fire 后 `_PROBE_TIMER` 非空，
    重排恒 no-op，第二个间隔内 embed 计数停在 1。
    """
    calls, recorded = probe_rig
    gear.ensure_half_open_probe_daemon()
    deadline = time.time() + 1.0
    while time.time() < deadline and calls["embed"] < 4:
        time.sleep(0.02)
    assert calls["embed"] >= 3, (
        f"探针在 20 个间隔窗口内只被调用 {calls['embed']} 次 —— "
        "一次性定时器的病没修干净：第一次 fire 后没有重挂"
    )
    assert calls["llm"] >= 3, (
        f"LLM 腿探针只被调用 {calls['llm']} 次 —— ensure 的 docstring 写着 "
        "'for all gear states'，但探测覆盖面没扩到 LLM 腿"
    )


def test_probe_timer_reference_cleared_after_fire(probe_rig):
    """fire 后的 Timer 引用必须被清掉（这是重挂能发生的前提）。"""
    gear.ensure_half_open_probe_daemon()
    first = gear._PROBE_TIMER
    assert first is not None and first.is_alive()
    deadline = time.time() + 1.0
    # 等第一个 tick 完成，引用要么已清（重排前）要么已换成新 Timer
    while time.time() < deadline:
        if gear._PROBE_TIMER is not first:
            break
        time.sleep(0.02)
    assert gear._PROBE_TIMER is not first, (
        "fire 完毕后 _PROBE_TIMER 仍指向旧 Timer 对象 —— 门卫会永远挡住重排"
    )


def test_probe_schedules_single_active_timer(probe_rig):
    """重挂修复不许引入反面病：同一时刻只允许一个活跃定时器（不堆积）。"""
    gear.ensure_half_open_probe_daemon()
    gear._schedule_probe_timer()      # 立刻再调一次重排
    gear._schedule_probe_timer()      # 再调
    alive = [t for t in [gear._PROBE_TIMER] if t is not None and t.is_alive()]
    # _PROBE_TIMER 只有一个槽位，断言它指向的定时器仍只有一个活跃实例：
    # 通过名字数线程
    named = [t for t in threading.enumerate() if t.name == "aiduMEI-gear-probe"]
    assert len(named) <= 1, f"探针线程堆积了 {len(named)} 个，定时器没做单活门卫"
