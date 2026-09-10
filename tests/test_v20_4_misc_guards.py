"""v20.4.0-alpha · 杂项守卫（G3/G5/G6）：SSH TOFU 告警、版本治理文档、架构横幅。

- P2-12（外审 Qwen #4）：router_usage 的 accept-new 是静默 TOFU，必须每进程
  WARNING 一次；显式设 AIDUMEM_ROUTER_SSH_STRICT=yes 后不再告警。
- P2-16（外审 Sonnet A1）：双轨版本政策必须成文且含关键规则句。
- P2-18（外审 Qwen B1 / Grok A1）：ARCHITECTURE.md 的 v14 时代标注必须前置到文首。
"""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestSshTofuWarning:
    def _reset(self, monkeypatch):
        import ducky.router_usage as ru
        monkeypatch.setattr(ru, "_TOFU_WARNED", False)
        monkeypatch.setenv("AIDUMEM_ROUTER_USAGE_ENABLED", "1")
        monkeypatch.delenv("AIDUMEM_ROUTER_SSH_HOSTS", raising=False)
        monkeypatch.delenv("AIDUMEM_ROUTER_SSH_KEY", raising=False)
        return ru

    def test_accept_new_warns_once(self, monkeypatch, caplog):
        ru = self._reset(monkeypatch)
        monkeypatch.delenv("AIDUMEM_ROUTER_SSH_STRICT", raising=False)
        import logging
        with caplog.at_level(logging.WARNING):
            ru.fetch_router_llm_usage()
            ru.fetch_router_llm_usage()  # 第二次不许再响
        warnings = [r for r in caplog.records if "accept-new" in r.getMessage()]
        assert len(warnings) == 1, f"accept-new 必须每进程 WARNING 恰一次：{len(warnings)}"

    def test_strict_yes_no_warning(self, monkeypatch, caplog):
        ru = self._reset(monkeypatch)
        monkeypatch.setenv("AIDUMEM_ROUTER_SSH_STRICT", "yes")
        import logging
        with caplog.at_level(logging.WARNING):
            ru.fetch_router_llm_usage()
        assert not [r for r in caplog.records if "accept-new" in r.getMessage()], \
            "已显式收紧的部署不许再被告警骚扰"


class TestVersioningDoc:
    def test_versioning_policy_exists_with_key_rules(self):
        path = os.path.join(_ROOT, "docs", "VERSIONING.md")
        assert os.path.isfile(path), "docs/VERSIONING.md 不存在（P2-16 未建立）"
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for needle in ("SERVICE_VERSION", "两段式", "alpha", "beta", "gamma", "不可变"):
            assert needle in text, f"VERSIONING.md 缺关键规则要素 {needle!r}"


class TestArchitectureBanner:
    def test_banner_at_top(self):
        with open(os.path.join(_ROOT, "docs/ARCHITECTURE.md"), encoding="utf-8") as f:
            head = f.read(600)
        assert "v14 时代" in head and "设计史" in head, \
            "ARCHITECTURE.md 文首缺「v14 时代 · 设计史」横幅（P2-18）"


class TestCiSecretScanIsNotFakeGreen:
    """P1-4 负向对照钉：gitleaks 自定义配置**整体替换**默认规则集 ——
    没有 useDefault 的配置 = 只有豁免没有检测 = 永真绿灯
    （2026-09-08 本机实测：缺这一句时连植入的 ghp_ 探针都扫不出）。"""

    def test_gitleaks_config_extends_default_rules(self):
        with open(os.path.join(_ROOT, ".github", "gitleaks.toml"), encoding="utf-8") as f:
            text = f.read()
        assert "useDefault" in text and "true" in text, \
            "gitleaks.toml 未 extend 默认规则集 —— 扫描形同虚设（P1-4 假绿灯陷阱）"

    def test_workflow_has_audit_jobs_but_no_new_triggers(self):
        with open(os.path.join(_ROOT, ".github", "workflows", "test.yml"), encoding="utf-8") as f:
            text = f.read()
        assert "pip-audit" in text and "gitleaks" in text, \
            "test.yml 缺 dependency-audit / secret-scan job（P1-4）"
        # 触发面策略（v20.4.1a 起，替代 2026-08-27 手动裁决）：四方外审
        # （Sonnet P0 / GPT Luna P1）指出「只留手动」让本 job 不在提交链路上。
        # 新策略：pull_request 全量 + push→main 精简（pytest job 以 if 跳过），
        # 保留 workflow_dispatch / workflow_call。判据与
        # tests/test_v20_ci_pipeline.py 的触发面守卫同源，两处须同步改。
        on_block = text.split("on:", 1)[1].split("jobs:", 1)[0]
        assert "pull_request" in on_block and "push" in on_block, (
            "test.yml 缺 pull_request/push 触发 —— v20.4.1a 起 CI 必须在提交链路上"
        )
        assert "workflow_dispatch" in on_block and "workflow_call" in on_block, \
            "手动/复用触发面被拆了"


class TestNoInlineStyleInFrontend:
    """P2-11（外审 Qwen #3）：CSP `style-src` 去掉 'unsafe-inline' 的前提 ——
    frontend/*.html 不得再出现 `style=` 内容属性（JS 的 el.style.x CSSOM
    写操作不受 CSP 管辖，不在此列）。新增 inline style 必须先收进
    frontend/css/，否则本守卫红给你看。

    v20.4.0（三方审计 P0-2 · 动态审计 🔴-2）：射程扩到 frontend/**/*.js ——
    上一轮收紧 CSP 的同一轮，守卫只扫 html 不扫 js，而 panels.js 有 66 处
    经 innerHTML 注入的内联 style 属性（CSP 对 style 内容属性的管辖不看
    来源，innerHTML 注入的照样拒），六面板布局塌掉、1788 条绿灯里一条
    都没红。「守卫射程病」第四次发作，这次把射程钉死在守卫里。"""

    _STYLE_ATTR = None  # 编译一次

    @classmethod
    def _pattern(cls):
        import re
        if cls._STYLE_ATTR is None:
            # HTML 内容属性形态：style= / style ＝，前面是空白（标签内属性位）。
            # JS 里的 el.style.x=、.style["x"]= 是 CSSOM 写操作，不匹配此形态。
            cls._STYLE_ATTR = re.compile(r"\sstyle\s*=")
        return cls._STYLE_ATTR

    def _scan(self, paths):
        offenders = []
        for path in paths:
            rel = os.path.relpath(path, _ROOT)
            with open(path, encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    for _m in self._pattern().finditer(line):
                        offenders.append(f"{rel}:{lineno}: {line.strip()[:80]}")
        return offenders

    def test_html_has_no_style_attributes(self):
        import glob
        offenders = self._scan(sorted(glob.glob(os.path.join(_ROOT, "frontend", "*.html"))))
        assert not offenders, (
            "frontend 出现 inline style 内容属性（P2-11 已清零，CSP 已不再容纳）：\n  "
            + "\n  ".join(offenders[:10]))

    def test_html_has_no_style_elements(self):
        """v20.5.0 preview（用户审计 · UI 修复）：`<style>` 元素形态。

        「守卫射程病」第五次发作：本守卫此前只拦 `style=` 内容属性，漏了
        `<style>` 元素——CSP `style-src 'self'`（无 unsafe-inline）对两种
        形态一律拒绝渲染。login.html 的 124 行与 index.html 的 45 行内联
        `<style>` 块就死在这一格：登录页样式塌成无样式白板（生产 2026-09-10
        实锤）。新代码的页面样式必须进 frontend/css/。"""
        import glob
        import re
        elem_re = re.compile(r"<style(?:\s[^>]*)?>")
        comment_re = re.compile(r"<!--.*?-->", re.S)
        offenders = []
        for path in sorted(glob.glob(os.path.join(_ROOT, "frontend", "*.html"))):
            rel = os.path.relpath(path, _ROOT)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            # 剥 HTML 注释后再扫：注释里的 "<style>" 字样（如本守卫来由的
            # 说明文字）不是元素，不违 CSP——守卫判据对结构不对字面。
            text = comment_re.sub("", text)
            for lineno, line in enumerate(text.splitlines(), 1):
                if elem_re.search(line):
                    offenders.append(f"{rel}:{lineno}: {line.strip()[:80]}")
        assert not offenders, (
            "frontend 出现内联 <style> 元素（CSP style-src 'self' 拒绝渲染，"
            "v20.5.0 preview 已清零）——请收进 frontend/css/：\n  "
            + "\n  ".join(offenders[:10]))

    def test_js_has_no_style_attributes_in_injected_markup(self):
        """P0-2：innerHTML/insertAdjacentHTML 模板串里的内联样式属性与 HTML
        里的同罪 —— style-src 'self'（无 unsafe-inline）一律拒绝渲染。

        豁免且只豁免 vendor/（第三方压缩库，逐文件点名不给目录级永久盲区）：
        echarts.min.js 的命中是压缩后的 CSSOM 赋值语句形态，不是本仓
        手写的注入模板，也改不了；它在真实浏览器下的 CSP 行为由
        实机验收（六面板零 violation）兜底，不归本静态守卫管。"""
        import glob
        paths = [p for p in sorted(glob.glob(
            os.path.join(_ROOT, "frontend", "**", "*.js"), recursive=True))
            if os.path.basename(p) != "echarts.min.js"]
        assert any(p.endswith("panels.js") for p in paths), "射程丢了 panels.js"
        offenders = self._scan(paths)
        assert not offenders, (
            f"frontend JS 注入的标记里有 {len(offenders)} 处内联 style 属性 —— "
            "CSP style-src 'self' 下浏览器直接拒绝，面板布局塌掉（动态审计 🔴-2）。"
            "动态量走 CSSOM（el.style.x = …），静态样式收进 frontend/css/：\n  "
            + "\n  ".join(offenders[:10]))
