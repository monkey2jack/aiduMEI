#!/usr/bin/env python3
"""相关性闸门（memory_gate）的实体词表回归测试。

血训（v15）：AIDUMEM_ENTITY_KEYWORDS 原本在 import 时固化成正则，
一旦模块比 setenv 先加载（systemd 单元漏 Environment= 就会这样），
自定义实体词永久失效，且全程静默 —— 涉及自己人名/项目代号的查询
一律被判 no_signal 不召回。本测试锁死惰性构建 + 热更新行为。

运行：python3 tests/test_memory_gate_entities.py
"""
import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE_PATH = os.path.join(REPO, "ducky", "pipeline", "memory_gate.py")

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}{(' — ' + detail) if detail else ''}")


def load_gate():
    spec = importlib.util.spec_from_file_location("_mg", GATE_PATH)
    assert spec is not None and spec.loader is not None, f"无法加载 {GATE_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    # 关键：import 时故意不设环境变量，模拟 systemd 漏配的现场
    os.environ.pop("AIDUMEM_ENTITY_KEYWORDS", None)
    g = load_gate()

    # 门控有 15 秒热缓存（上轮要记忆 + 本轮 <12 字追问就沿用），
    # 逐条独立判定前必须清掉，否则测的是缓存不是规则。
    def reason(q: str) -> str:
        g.reset_gate_cache()
        return g.relevance_check(q)["reason"]

    st = g.entity_keywords_status()
    check("未配置时 status 上报 configured=False",
          st["configured"] is False and st["count"] == 0, str(st))
    check("未配置时自定义实体查询判 no_signal", reason("老张今天怎么样呀") == "no_signal")
    check("未配置时通用自指模式仍生效", reason("还记得我上次说的偏好吗") != "no_signal")

    # import 之后再 setenv —— 旧实现在这里永久失效
    os.environ["AIDUMEM_ENTITY_KEYWORDS"] = "老张|小李|ProjectBox"
    st = g.entity_keywords_status()
    check("import 后 setenv 能被感知（惰性构建）",
          st["configured"] is True and st["count"] == 3, str(st))
    check("中文自定义实体命中 self_reference", reason("老张今天怎么样呀") == "self_reference")
    check("英文项目代号命中 self_reference", reason("ProjectBox 部署到哪了") == "self_reference")

    # 词表热更新：改了值要立刻重建，不能吃旧缓存
    os.environ["AIDUMEM_ENTITY_KEYWORDS"] = "ProjectX"
    check("词表变更后旧词失效", reason("老张今天怎么样呀") == "no_signal")
    check("词表变更后新词生效", reason("ProjectX 进度如何") == "self_reference")

    # 元字符必须被转义，不能让部署方的输入破坏整条正则。
    # 注意 `|` 是词表分隔符，所以 "a(b|c)*" 会被切成 "a(b" / "c)*" 两个字面词。
    os.environ["AIDUMEM_ENTITY_KEYWORDS"] = "a(b|c)*|[bad"
    st = g.entity_keywords_status()
    check("含正则元字符的词表不炸", st["count"] == 3, str(st))
    check("元字符按字面量匹配", reason("hello [bad thing happened") == "self_reference")
    check("元字符不会被当正则展开", reason("hello abbb thing happened") == "no_signal")

    # 向后兼容：老代码可能直接 from ... import SELF_REFERENCE
    check("SELF_REFERENCE 模块级别名仍可用", bool(g.SELF_REFERENCE.search("我的生日")))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
