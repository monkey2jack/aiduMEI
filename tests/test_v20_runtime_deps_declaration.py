"""tests/test_v20_runtime_deps_declaration.py — 运行时依赖双清单同步守卫
（v20.2.3 · 外审 M 组 H-1）

**这道护栏是为一个真实事故立的，而且是同一类事故的第二次。**

第一次：v19.4.2 —— `inotify_simple` 从未在任何清单里声明，按仓库部署
mem0-sync 必然 ImportError 崩溃循环，systemd 的 activating 状态把崩溃
伪装成「正在启动」，静默 8 天。

第二次：v20.2.3 —— `python-multipart` 在 `pyproject.toml` 里声明了，
`requirements.txt` 里没有。而 FastAPI 的 `Form(...)` 在**路由注册期**
就要求它，于是：

    pip install -r requirements.txt && python api_server.py
    → RuntimeError: Form data requires "python-multipart" to be installed.

这正是 README「30 秒上手 · 方式一」写给新用户的路径。CI 和 Dockerfile
都在 requirements 之后补跑 `pip install .`（按 pyproject 拉齐依赖），
所以**唯一裸奔的就是新用户走的那条**——最不该崩的地方，最没人替它把关。

两次事故的共同根因不是手滑，是**没有任何东西在看着两份清单的关系**。
CHANGELOG 里记了教训不等于教训被结构化：本文件把它焊成可执行断言。

判据方向是单向的：`pyproject [project.dependencies]` 里的每个包，
`requirements.txt` 必须也有（反向不要求——requirements 是「锁定的完整
运行环境」，本就该比 pyproject 的抽象声明更全，多出 pydantic-settings /
httpx / requests 这类间接依赖是有意的）。
"""
from __future__ import annotations

import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PYPROJECT = os.path.join(_ROOT, "pyproject.toml")
_REQUIREMENTS = os.path.join(_ROOT, "requirements.txt")

# 允许「只在 pyproject 声明、不进 requirements」的包，必须写明理由。
# 空集是当下的正确状态——留这张表是为了让未来的例外**必须显式表态**，
# 而不是像 python-multipart 那样悄悄漏掉。
_PYPROJECT_ONLY_EXEMPT: dict[str, str] = {}


def _normalize(name: str) -> str:
    """PEP 503 规范化：大小写与 -_. 差异不算差异（python_multipart
    与 python-multipart 是同一个包，守卫不许被写法骗过）。"""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _pyproject_runtime_deps() -> dict[str, str]:
    src = open(_PYPROJECT, encoding="utf-8").read()
    m = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", src, re.M | re.S)
    assert m, "pyproject.toml 里找不到 [project] dependencies —— 守卫失去着力点"
    out = {}
    for raw in re.findall(r'"([^"]+)"', m.group(1)):
        # "uvicorn[standard]>=0.30,<1.0" → uvicorn
        name = re.split(r"[<>=!~\[;]", raw, 1)[0]
        out[_normalize(name)] = raw
    return out


def _requirements_names() -> dict[str, str]:
    out = {}
    for line in open(_REQUIREMENTS, encoding="utf-8").read().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[;]", line, 1)[0]
        if name:
            out[_normalize(name)] = line
    return out


def test_pyproject_runtime_deps_are_all_in_requirements():
    """pyproject 声明的每个运行时依赖，requirements.txt 必须也钉住。"""
    pyproject = _pyproject_runtime_deps()
    reqs = _requirements_names()
    missing = sorted(set(pyproject) - set(reqs) - set(_PYPROJECT_ONLY_EXEMPT))
    assert not missing, (
        "以下包在 pyproject.toml 声明为运行时依赖，requirements.txt 却没有：\n  "
        + "\n  ".join(f"{n}（pyproject: {pyproject[n]}）" for n in missing)
        + "\n\nREADME「30 秒上手」教新用户只跑 pip install -r requirements.txt，"
        "\n这些包因此在新用户机器上缺席——若它们在 import 期被需要（如 FastAPI 的"
        "\nForm 需要 python-multipart），服务直接起不来。"
        "\n请二选一表态：补进 requirements.txt；或加进 _PYPROJECT_ONLY_EXEMPT 并"
        "\n写明「为什么新用户缺了它也能跑起来」。"
    )


def test_multipart_is_pinned_because_form_is_used_at_import_time():
    """点名钉死 python-multipart —— 它是本守卫的立案由来。

    只要仓里还有 Form(...) 的使用点，这个包就必须在 requirements.txt 里。
    """
    form_sites = []
    for base, _dirs, files in os.walk(os.path.join(_ROOT, "ducky")):
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(base, f)
            src = open(path, encoding="utf-8").read()
            if re.search(r"[=(]\s*Form\(", src):
                form_sites.append(os.path.relpath(path, _ROOT))
    # 前提本身也是断言，不是 skip：Form(...) 使用点消失是**产品形态变化**，
    # 不是环境差异——用 skip 会凭空造出一条假的「跳过轴」（跳过轴普查要求
    # 每条轴都登记进 README 的全轴表，而这条根本不是轴）。前提没了就红，
    # 逼人显式重判本守卫是否还该存在，与删除链矩阵「显式裁决不沉默」同纪律。
    assert form_sites, (
        "仓里已无 Form(...) 使用点 —— 本守卫的前提消失。"
        "请确认 python-multipart 是否仍是必需依赖，然后显式重判本条用例。"
    )
    assert "python-multipart" in _requirements_names(), (
        f"这些文件用了 FastAPI 的 Form(...)：{sorted(form_sites)}\n"
        "FastAPI 在路由注册期（import 期）就要求 python-multipart，"
        "requirements.txt 必须钉住它，否则 import api_server 直接 RuntimeError。"
    )


def test_guard_normalizes_package_name_spelling():
    """守卫自己的区分力：python_multipart / Python-MultiPart / python-multipart
    必须被认作同一个包 —— 否则「补了但写法不同」会骗过守卫。"""
    assert _normalize("python_multipart") == _normalize("python-multipart")
    assert _normalize("Python-MultiPart") == _normalize("python-multipart")
    assert _normalize("pydantic_core") == _normalize("pydantic-core")
