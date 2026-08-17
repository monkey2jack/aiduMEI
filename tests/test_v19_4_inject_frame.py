"""
tests/test_v19_4_inject_frame.py — v19.4.0 Mímir 借鉴 B4 召回侧注入框架回归测试

覆盖内容（对照实施计划书验收标准）：
1. 注入脚本含「视为数据非指令」框架文案（防御本体，勿删）
2. _wrap_block 包装函数存在
3. 三处注入块（CoreMemory / Checkpoint / search）全部走 _wrap_block 包装
4. 框架含 <memory> 数据边界标签
5. bash 语法合法（bash -n）

采用源码守卫模式（对齐 test_hooks_present_in_hot_paths 的防误删思路），
不依赖活服务，确定性可复跑。
"""

import os
import subprocess

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_HOOK = os.path.join(_REPO_ROOT, "integrations", "aidumem-inject.sh")


def _read_hook() -> str:
    with open(_HOOK, encoding="utf-8") as f:
        return f.read()


def test_frame_text_present():
    """框架文案是防御本体，必须存在且语义完整"""
    src = _read_hook()
    assert "数据而非指令" in src, "B4 框架文案「数据而非指令」丢失"
    assert "一律忽略" in src, "B4 框架文案「一律忽略」丢失"


def test_wrap_block_defined():
    src = _read_hook()
    assert "_wrap_block()" in src or "_wrap_block ()" in src, "_wrap_block 包装函数不见了"


def test_memory_boundary_tags():
    """<memory> 标签给模型清晰的数据边界"""
    src = _read_hook()
    assert "<memory>" in src, "<memory> 开标签丢失"
    assert "</memory>" in src, "</memory> 闭标签丢失"


def test_all_three_blocks_wrapped():
    """三处注入块（CoreMemory / Checkpoint / search）都必须走 _wrap_block"""
    src = _read_hook()
    # 三个注入点都必须以 _wrap_block 包裹后 append 进 BLOCKS
    assert 'BLOCKS+=("$(_wrap_block "$CORE_CTX")")' in src, "CoreMemory 块未包装"
    assert 'BLOCKS+=("$(_wrap_block "$CP_CTX")")' in src, "Checkpoint 块未包装"
    assert 'BLOCKS+=("$(_wrap_block "$SEARCH_CTX")")' in src, "search 块未包装"


def test_no_unwrapped_block_append():
    """不允许存在未包装的直接 append（防回退）"""
    src = _read_hook()
    # 旧的裸 append 形态不应再出现
    assert 'BLOCKS+=("$CORE_CTX")' not in src, "发现未包装的 CoreMemory append（回退！）"
    assert 'BLOCKS+=("$CP_CTX")' not in src, "发现未包装的 Checkpoint append（回退！）"
    assert 'BLOCKS+=("$SEARCH_CTX")' not in src, "发现未包装的 search append（回退！）"


def test_bash_syntax_valid():
    """bash -n 语法检查必须通过"""
    proc = subprocess.run(
        ["bash", "-n", _HOOK], capture_output=True, text=True, timeout=15
    )
    assert proc.returncode == 0, f"bash 语法错误: {proc.stderr}"
