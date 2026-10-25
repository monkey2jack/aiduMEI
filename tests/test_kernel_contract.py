"""f0.6+ · WP1 记忆内核守卫。

四道判据,全部带负向对照(守卫判据纪律):
  1. 默认内核=mem0(契约现状);
  2. 未知内核名 fail-fast(绝不静默回退——换引擎是部署事故必须响);
  3. planned 内核(native)请求时如实抛 KernelNotImplemented(不回退);
  4. ducky/ 内禁止绕过 memory_kernel 直 import mem0(白名单:mem0_runtime
     本体+impl_mem0 实现;AST 盲扫,不字符串 grep)。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# 允许直接 import mem0 的白名单(相对 ducky/),逐项带理由——棘轮:只减不增:
#   mem0_runtime.py = 现行实现真身(初始化/单例/补丁全在那)
#   memory_kernel/impl_mem0.py = kernel 实现适配器(转调真身)
#   mem0_patches.py = 补丁层本职(给 mem0 行为打补丁,不摸它摸谁)
#   mutation_history.py→mutation_journal.py = journal 包装 mem0 写入方法(拦截点)
#   tombstone.py = 墓碑语义包装 mem0 delete(拦截点)
_WHITELIST = {
    "mem0_runtime.py": "现行实现真身",
    "memory_kernel/impl_mem0.py": "kernel 实现适配器",
    "mem0_patches.py": "补丁层本体(职责即修改 mem0 行为)",
    "mutation_journal.py": "journal 拦截 mem0 写入面(包装器)",
    "tombstone.py": "墓碑语义包装 mem0 delete(包装器)",
}


def test_default_kernel_is_mem0(monkeypatch):
    monkeypatch.delenv("AIDUMEI_MEMORY_KERNEL", raising=False)
    from ducky import memory_kernel
    assert memory_kernel.configured_kernel() == "mem0"
    # 负向对照:设了非默认值时确实生效(证明上一条不是写死的永真)
    monkeypatch.setenv("AIDUMEI_MEMORY_KERNEL", "native")
    assert memory_kernel.configured_kernel() == "native"


def test_unknown_kernel_fails_fast(monkeypatch):
    monkeypatch.setenv("AIDUMEI_MEMORY_KERNEL", "typo-kernel")
    from ducky import memory_kernel
    with pytest.raises(ValueError, match="未知记忆内核"):
        memory_kernel.get_memory()
    # 负向对照:合法名不抛未知错(但 native 会抛未实现——另一条管)
    monkeypatch.setenv("AIDUMEI_MEMORY_KERNEL", "mem0")
    memory_kernel.configured_kernel()  # 不 raise 即可


def test_planned_kernel_raises_not_implemented(monkeypatch):
    monkeypatch.setenv("AIDUMEI_MEMORY_KERNEL", "native")
    from ducky import memory_kernel
    from ducky.memory_kernel.base import KernelNotImplemented
    with pytest.raises(KernelNotImplemented):
        memory_kernel.get_memory()
    # 注册表面如实:native 必须显示 planned 而非 ready
    table = memory_kernel.available_kernels()
    assert table.get("mem0") == "ready" and table.get("native") == "planned"


def test_no_mem0_import_bypass_outside_whitelist():
    """AST 盲扫 ducky/:绕过 kernel 直 import mem0 = 红灯。

    负向对照由白名单本身充当:mem0_runtime.py/impl_mem0.py 里确实存在
    mem0 import(可运行时验证),扫描器若把白名单外的也无视,本条会因
    「白名单内必命中」探针而红——判据证明不是隐形的。
    """
    import ducky.mem0_runtime as mrt
    src_runtime = pathlib.Path(mrt.__file__).read_text(encoding="utf-8")
    assert "mem0" in src_runtime, "扫描器探针失效:白名单文件已不含 mem0?"

    offenders = []
    ducky_dir = _ROOT / "ducky"
    for py in sorted(ducky_dir.rglob("*.py")):
        rel = py.relative_to(ducky_dir).as_posix()
        if rel in _WHITELIST or rel.startswith("memory_kernel"):
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n == "mem0" or n.startswith("mem0.") for n in names):
                offenders.append(rel)
    assert not offenders, (
        f"绕过 memory_kernel 直 import mem0 的新触点(只许经 get_memory 句柄;"
        f"存量白名单 {_WHITELIST} 只减不增):{offenders}"
    )
