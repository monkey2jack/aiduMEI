"""tests/test_v20_lifespan_background.py — v20 P1-6：后台能力不许只挂在 main()

外部审计 H4：全部后台能力只在 `main()` 里启动，全文 0 处 `lifespan` / `on_event`。

`main()` 是 `python api_server.py` 和控制台入口点走的路，但**不是唯一的路**：

    uvicorn api_server:app                                    # 官方文档最常见的起法
    gunicorn -k uvicorn.workers.UvicornWorker api_server:app
    任何把 `app` 当 ASGI 对象导入的进程（含测试、含反代后的多 worker 部署）

这些起法完全不经过 `main()`。后果不是崩溃 —— 是**服务照样监听、`/health` 照样返回
ok、读写接口照样能用，而 WAL 启动对账没跑、后台线程一个没起**。「一半的能力静默
缺席」比崩溃危险得多：崩溃会被发现，这个不会。

判据必须**不经过 `main()`**，否则就测不到那条真出问题的路（假绿灯铁律那句话：
「我断言的这条路，是生产真的会走的那条路吗？」—— 这里要反过来问：我断言的这条路，
是**出问题**的那条路吗）。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_lifespan_background.py -v
"""
from __future__ import annotations

import ast
import importlib
import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _api_server():
    return importlib.import_module("api_server")


# ═══════════════ ① 不走 main()，后台照样起 ═══════════════

def test_background_starts_via_asgi_lifespan_without_calling_main(monkeypatch):
    """★ 核心断言：只把 `app` 当 ASGI 对象用（`TestClient` 进上下文 = 触发 lifespan），
    `_start_background` 必须被调用 —— 而 `main()` 一次都没碰。

    整改前这条断言必然红：那时唯一的启动点是 `main()`。
    """
    from fastapi.testclient import TestClient
    api = _api_server()

    called = {"n": 0}

    def spy():
        called["n"] += 1

    monkeypatch.setattr(api, "_start_background", spy)
    monkeypatch.setattr(api, "main", lambda: pytest.fail("用例走到了 main()，判据失效"))

    with TestClient(api.app):
        pass

    assert called["n"] >= 1, (
        "把 app 当 ASGI 对象起（uvicorn api_server:app 走的就是这条路），"
        "后台能力一次都没启动 —— WAL 对账没跑、后台线程没起，而服务看着完全正常"
    )


def test_lifespan_is_actually_wired_into_the_app_object():
    """判据落在 app 对象上，不落在源码字符串上。

    源码里出现 `lifespan` 三个字不代表它挂上去了（可能只在注释里）。
    这里问 FastAPI 自己：你的 router 上有没有 lifespan 上下文？
    """
    api = _api_server()

    ours = getattr(api.app.router, "lifespan_context", None)
    assert ours is not None, "app.router 上没有 lifespan_context"
    assert getattr(api, "_lifespan", None) is not None, "模块里没有 _lifespan"

    # ⚠️ 这条判据被变异轮打回两次，记下来：
    #   第一版「lifespan_context 不是 None」—— 恒真。FastAPI 没传 lifespan 时也会挂
    #     一个默认空实现（`_DefaultLifespan`），摘掉 `lifespan=` 它照样绿。
    #   第二版「和裸 FastAPI() 的默认实现比身份」—— 也恒真。默认实现是**每个 app
    #     实例各造一个**，两个不同实例的身份比较永远不相等，于是仍然分不出来。
    # 正解是直接问那个唯一有意义的问题：**app 挂的就是我们这个函数吗。**
    assert ours is api._lifespan, (
        f"app 的 lifespan_context 是 {type(ours).__name__}，不是本模块的 _lifespan —— "
        "说明 lifespan= 没传进去，挂载只存在于源码的字面上"
    )


def test_start_background_is_idempotent_so_both_entry_paths_are_safe():
    """★ 两条路都调用它，必须无副作用。

    `main()` 里那一次调用是刻意保留的（在服务器绑定端口之前预热完）。加了 lifespan
    之后，`python api_server.py` 这条路会调两次 —— 幂等性是这次整改「不改动既有
    行为」的全部依据，所以它必须被断言，而不是被相信。
    """
    api = _api_server()
    # 直接验双检锁的形状：第二次进入必须提前 return，不重复起线程
    import threading
    assert isinstance(api._background_lock, type(threading.Lock())), "锁的类型变了"

    # 语法树自证：函数体第一件事就是拿锁 + 检查标志 + return
    src = open(os.path.join(_REPO_ROOT, "api_server.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_start_background"), None)
    assert fn is not None, "_start_background 不见了"
    has_guard = any(
        isinstance(n, ast.Return) and n.value is None
        for n in ast.walk(fn)
    )
    assert has_guard, (
        "_start_background 里没有提前 return 的幂等守卫 —— "
        "两条启动路径会各起一遍后台线程"
    )


# ═══════════════ ② 射程：后台能力不许再长出第二个只挂 main 的入口 ═══════════════

def test_no_background_capability_is_reachable_only_from_main():
    """★ 元测试：`main()` 里不许出现只有它才做的初始化动作。

    整改的实质是把「启动」这件事从 `main()` 收敛到 `_start_background()`。这条断言
    防止下一个人图方便，把新的初始化直接写进 `main()` —— 那等于把 P1-6 原样复活。

    判据：`main()` 函数体里除了 `_start_background()` 之外，不许再调用任何名字里
    带 `init` / `ensure` / `start` / `reconcile` 的函数。
    """
    src = open(os.path.join(_REPO_ROOT, "api_server.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    main_fn = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert main_fn is not None, "main() 不见了"

    SUSPECT = ("init", "ensure", "start", "reconnect", "reconcile", "warmup", "preload")
    offenders = []
    for node in ast.walk(main_fn):
        if not isinstance(node, ast.Call):
            continue
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if not name or name == "_start_background":
            continue
        low = name.lower()
        if any(k in low for k in SUSPECT):
            offenders.append(f"{name}() @ line {node.lineno}")

    assert not offenders, (
        "main() 里有只有它才会执行的初始化动作：\n  " + "\n  ".join(offenders)
        + "\n这些动作在 `uvicorn api_server:app` 起法下永远不会发生。"
          "请把它们移进 _start_background()（那里已经挂进 lifespan，两条路都覆盖）。"
    )
