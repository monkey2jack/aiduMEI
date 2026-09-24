"""f0.1+ 本能升格（instinct_graduation）删除安全整改守卫。

自查 auto_merge_similar 同类缺陷时找到的第二处：``graduate_to_skill``
按 category 把 ≤10 条原始记忆蒸馏成 1 条技能，然后**删掉原始**，且
直接调 mem0 原生 ``memory.delete()`` —— 绕过 tombstone，删了找不回来。

查代码时另发现一处更重的：``memory.add`` 的返回值**一眼都不看**就往下删。
mem0 在抽取返回空时会静默丢弃、不抛异常，于是可能技能没写成、原始记忆
照删不误 —— 净蒸发，日志还报「毕业成功」。

与 auto_merge_similar 的区别（不要照搬那边的改法）：
「升格」按 category 聚类在语义上是**成立的**（把同类经验提炼成技能），
所以这里**不加内容相似度判据**；要修的是「删得掉但找不回」和
「没写成也照删」，以及把破坏性 API 的默认值掰回安全侧。
"""
from __future__ import annotations

import ast
import os

from ducky.instinct_graduation import _add_succeeded

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "ducky", "instinct_graduation.py")


def _func(name: str) -> ast.FunctionDef:
    tree = ast.parse(open(_SRC, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} 不在 {_SRC} 里 —— 守卫失去着力点")


def _calls(node: ast.AST) -> set:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def test_graduation_deletes_through_tombstone():
    """升格删原始记忆必须留墓碑、走级联删除。"""
    names = _calls(_func("graduate_to_skill"))
    assert "snapshot_before_delete" in names, (
        "graduate_to_skill 删除前没留 tombstone —— 一次毕业删 10 条且不可恢复"
    )
    assert "cascade_delete_memory" in names, "没走 cascade_delete_memory，级联清理会漏"


def test_graduation_no_longer_calls_raw_mem0_delete():
    """钉死：不许再出现 memory.delete() —— 那条路绕过快照。"""
    for n in ast.walk(_func("graduate_to_skill")):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "delete" \
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "memory":
            raise AssertionError("graduate_to_skill 又出现 memory.delete()，绕过了墓碑")


def test_graduation_verifies_write_before_deleting():
    """钉死：必须先确认技能写入成功，才允许删原始记忆。"""
    names = _calls(_func("graduate_to_skill"))
    assert "_add_succeeded" in names, (
        "没有校验 memory.add 的结果 —— mem0 抽取返空时静默丢弃，"
        "会出现『技能没写成、原始记忆全删光』的净蒸发"
    )


def test_add_succeeded_rejects_every_silent_failure_shape():
    """`_add_succeeded` 必须认得出 mem0 的各种静默失败形态。"""
    # 失败形态：一律判 False
    for bad in (None, {}, {"results": []}, [], "", 0, False):
        assert _add_succeeded(bad) is False, f"静默失败形态 {bad!r} 被当成写入成功"
    # 成功形态：一律判 True（负向对照 —— 否则上面的 False 可能是恒定值）
    for good in ({"results": [{"id": "m1"}]}, {"id": "m1"}, [{"id": "m1"}]):
        assert _add_succeeded(good) is True, f"正常写入 {good!r} 被误判为失败"


def test_graduate_endpoint_defaults_to_dry_run():
    """破坏性端点的默认值必须在安全侧：`POST /graduate` 默认只预览。"""
    src = open(os.path.join(_REPO, "ducky", "routes_v8.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "graduate_instincts":
            defaults = dict(zip(
                [a.arg for a in node.args.args][-len(node.args.defaults):],
                node.args.defaults,
            ))
            dr = defaults.get("dry_run")
            assert dr is not None, "graduate_instincts 的 dry_run 没有默认值"
            assert isinstance(dr, ast.Constant) and dr.value is True, (
                f"dry_run 默认应为 True（只预览），实际 {getattr(dr, 'value', dr)!r} —— "
                "不带参数调一次就真删，默认值方向错了"
            )
            return
    raise AssertionError("graduate_instincts 不在 routes_v8.py 里 —— 守卫失去着力点")
