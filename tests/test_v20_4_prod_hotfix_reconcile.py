"""生产热修 3219f8c4e 并回本轮的守卫（2026-09-08）。

**这三条守卫的来历比它们本身重要。**

2026-09-07 16:18，猴哥在杭州生产机上直接改了两处并就地提交（`3219f8c4e`），
从未推送到任何远端：

1. `api_server.py` 的 CSP `script-src 'self'` 加回 `'unsafe-inline'`——
   因为 `frontend/index.html` 与 `frontend/login.html` 各有一段 inline
   `<script>`，被 CSP 打死了：登录页的六边形背景、版本徽章、口令提示全不动，
   主控台的未登录跳转也不动。
2. `ducky/pipeline/memory_vision.py` 的请求头补 `User-Agent`——10router
   多模态网关不认没有 UA 的请求。

**为什么本轮必须留守卫，而不是只把代码改回来**：P2-11 这一轮做的正是
「CSP 去 unsafe-inline」，它写了 `TestNoInlineStyleInFrontend` 钉住 inline
**style** 不许回流，却没想到同一个 CSP 头上还有 **script** 那一格——
射程只盖了自己刚走过的那条路（SOP 铁律 12：守卫的射程）。于是本地一直带着
一个「/ui 的 inline script 在浏览器里是死的」的缺陷，而生产上靠一个
未入版本控制的热修撑着。本地测试全绿，缺陷照样在线上（SOP 铁律 6：假绿灯）。

所以这里钉三面，缺一面就还会从那一面漏：
- HTML 面：不许有 inline `<script>`（正向要求，逼新代码走外部 .js）
- 头面：CSP 实测响应里 `script-src`/`style-src` 都不许出现 `'unsafe-inline'`
  （反向要求，防止有人图省事把热修那一行再抄回来）
- 行为面：Vision 请求真的带上 UA，且 UA 里的版本号取自 `version.py`
  而不是手写常量（热修里写死的是 `20.3`，版本一升就成了假话——SOP 铁律 18）
"""
import glob
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 带 src= 的 <script src=...> 是外部脚本，CSP 'self' 放行；不带 src 的是
# inline 块，被 script-src 'self' 拒绝执行。判据只看这一件事。
_SCRIPT_OPEN = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
_HAS_SRC = re.compile(r"\bsrc\s*=", re.IGNORECASE)


class TestNoInlineScriptInFrontend:
    """HTML 面：frontend/*.html 里每个 `<script>` 都必须带 `src=`。"""

    def test_every_script_tag_is_external(self):
        offenders = []
        scanned = []
        for path in sorted(glob.glob(os.path.join(_ROOT, "frontend", "*.html"))):
            scanned.append(os.path.basename(path))
            with open(path, encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    for m in _SCRIPT_OPEN.finditer(line):
                        if not _HAS_SRC.search(m.group(1)):
                            offenders.append(
                                f"{os.path.basename(path)}:{lineno}: {line.strip()[:80]}")
        # 射程自检：扫了 0 个文件的「0 命中」是假绿灯（macOS push_gate 的旧账）
        assert scanned, "frontend/*.html 一个都没扫到，判据失去射程"
        assert not offenders, (
            "frontend 出现 inline <script>（CSP script-src 'self' 会拒绝执行它，"
            "生产 3219f8c4e 就是为此加回 unsafe-inline）——请收进 frontend/js/：\n  "
            + "\n  ".join(offenders[:10])
            + f"\n（已扫 {len(scanned)} 个 HTML：{', '.join(scanned)}）")


class TestCspHasNoUnsafeInline:
    """头面：实测响应头里 script-src / style-src 都不许出现 'unsafe-inline'。

    P2-11 只改了源码字符串，没有任何测试读过真实响应头——这里补上，
    顺带让「把热修那行抄回来」这个动作有代价。
    """

    def _csp(self):
        from fastapi.testclient import TestClient

        from api_server import app
        r = TestClient(app).get("/livez")
        assert "Content-Security-Policy" in r.headers, "响应必须带 CSP 头"
        return r.headers["Content-Security-Policy"]

    @pytest.mark.parametrize("directive", ["script-src", "style-src"])
    def test_directive_forbids_unsafe_inline(self, directive):
        csp = self._csp()
        m = re.search(rf"{directive}\s+([^;]+)", csp)
        assert m, f"CSP 里找不到 {directive}：{csp}"
        value = m.group(1)
        assert "unsafe-inline" not in value, (
            f"{directive} 又出现 'unsafe-inline'（值={value.strip()!r}）。"
            "inline 内容请收进 frontend/js/ 或 frontend/css/，不要放宽 CSP。")


class TestVisionRequestIdentifiesItself:
    """行为面：Vision 出网请求带 UA，且版本号取自 version.py。"""

    def test_vision_post_sends_versioned_user_agent(self, monkeypatch):
        import ducky.engine_mode as engine_mode
        import ducky.pipeline.memory_vision as mv
        from ducky.version import SERVICE_VERSION

        captured = {}

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "一张图"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                  "total_tokens": 2}}

        def _fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            return _Resp()

        monkeypatch.setattr(mv, "get_vision_config", lambda: {
            "api_key": "sk-test-not-a-real-key", "openai_base_url":
            "https://example.invalid/v1", "model": "test-vision"})
        monkeypatch.setattr(mv, "requests", type("R", (), {"post": staticmethod(_fake_post)}))
        monkeypatch.setattr(engine_mode, "cloud_egress_allowed", lambda _f: True)

        out = mv.extract_vision_caption("https://example.invalid/a.jpg")
        assert out == "一张图", f"桩路径没走通，实得 {out!r}"

        headers = captured.get("headers", {})
        ua = headers.get("User-Agent")
        assert ua, ("Vision 请求必须带 User-Agent（10router 网关不认没有 UA 的请求，"
                    f"生产 3219f8c4e 为此打过热修）；实得头={sorted(headers)}")
        assert SERVICE_VERSION in ua, (
            f"UA 里的版本号必须取自 version.py（当前 {SERVICE_VERSION}），"
            f"不许写死——实得 {ua!r}。热修里写死的是 20.3，版本一升就成假话。")


class TestReferencedScriptsResolve:
    """把 inline 收进外部文件之后，新的失败形态是「引用在、文件不在」——
    HTML 里 `<script src>` 指向一个 404，浏览器同样什么都不执行，而
    `TestNoInlineScriptInFrontend` 会照样绿（它只管有没有 inline）。
    所以补这一层：每个被引用的 src 必须在盘上、且真能从 /ui/ 服务出去。
    """

    def _srcs(self):
        pairs = []
        for path in sorted(glob.glob(os.path.join(_ROOT, "frontend", "*.html"))):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for m in re.finditer(r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']",
                                 text, re.IGNORECASE):
                src = m.group(1)
                if src.startswith(("http://", "https://", "//")):
                    continue  # 外链自托管政策另有守卫，这里只管本地资源
                pairs.append((os.path.basename(path), src))
        return pairs

    def test_every_referenced_script_exists_on_disk(self):
        pairs = self._srcs()
        assert pairs, "frontend/*.html 里一个本地 <script src> 都没扫到，判据失去射程"
        missing = []
        for html, src in pairs:
            rel = src.split("?", 1)[0]  # 去掉 ?v=8 缓存串
            if not os.path.isfile(os.path.join(_ROOT, "frontend", rel)):
                missing.append(f"{html} 引用 {src}，但 frontend/{rel} 不存在")
        assert not missing, "\n  ".join(["引用指向不存在的脚本："] + missing)

    def test_every_referenced_script_is_served(self):
        from fastapi.testclient import TestClient

        from api_server import app
        client = TestClient(app)
        bad = []
        pairs = self._srcs()
        assert pairs, "扫到 0 个引用，判据失去射程"
        for html, src in pairs:
            rel = src.split("?", 1)[0]
            r = client.get(f"/ui/{rel}")
            if r.status_code != 200 or not r.content:
                bad.append(f"{html} 引用 {src} → /ui/{rel} 得 {r.status_code}"
                           f"（{len(r.content)} 字节）")
        assert not bad, "\n  ".join(["被引用的脚本服务不出来："] + bad)
