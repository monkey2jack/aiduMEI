"""内核契约:f0.6+ 只「以结构钉住」——f0.6++ 的 native 实现落地时升级为 ABC。

为什么现在不用抽象基类:调用方 64 个触点在长期使用中把 Memory 对象的
真实形状(add/search/update/delete/get_all + 若干属性)当成了事实契约,
但它们**不是**处处用到全部方法。此刻强行用 ABC@property 全钉,会逼出
一套"看起来正式、其实与真实用法对不上"的接口——比没有更危险(data-
driven 契约优先于纸面契约)。f0.6++ native 落地时按其真实差异再吸收进
ABC;本模块此刻提供:

  - KernelContract:记忆内核必须能回答的能力问题集合(结构协定);
  - kernel_not_implemented():已注册但未完工的实现统一抛这个,
    fail-fast + 名称如实,绝不静默回退到 mem0(静默=换引擎不告知)。
"""
from __future__ import annotations


class KernelNotImplemented(NotImplementedError):
    """已注册、未完工的内核被请求时抛出——如实,不回退。"""

    def __init__(self, name: str):
        super().__init__(
            f"memory kernel {name!r} 已注册但尚未实现(f0.6++ 计划);"
            f"AIDUMEI_MEMORY_KERNEL 当前指向它。回到默认请 unset 该变量。"
        )
        self.kernel = name


def kernel_not_implemented(name: str):
    def _raise(*_a, **_kw):
        raise KernelNotImplemented(name)
    return _raise


#: 契约方法面(f0.6+ 结构协定;native 落地时升级为 ABC)。
#: 判据来源:全仓 get_memory() 触点的真实调用面 mem.add/search/update/
#: delete/get_all/get + vector_store 属性(dual_index 借道)。
CONTRACT_METHODS = ("add", "search", "update", "delete", "get_all", "get")
CONTRACT_ATTRS = ("vector_store",)


class KernelContract:
    """结构自检:任一实现必须在注册时自证方法面齐全(注册表强制调用)。"""

    @staticmethod
    def verify(obj) -> list[str]:
        missing = [m for m in CONTRACT_METHODS if not callable(getattr(obj, m, None))]
        missing += [a for a in CONTRACT_ATTRS if not hasattr(obj, a)]
        return missing
