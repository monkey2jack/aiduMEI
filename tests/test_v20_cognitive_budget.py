"""tests/test_v20_cognitive_budget.py — v20 P1-5：认知类调用的输出预算

用户视角审计 A2 点名三处 `max_tokens=512`：`ducky/governance.py:293`、
`ducky/self_edit.py:189`、`ducky/instinct_graduation.py:64`（第三处是**函数默认
参数**，上一轮我漏数了，认）。

但「三处 512」只是症状。根因是两条：

① **推理模型的思考和输出共享同一个 max_tokens。** 预算给小了，思考先把它吃光，
   `content` 回空串、`finish_reason=length`（v19.4.0 生产实测 🔴-B）。
   `llm_client` 早就有截断检测 + ×4 放大重试兜底 —— 但重试封顶
   `min(max_tokens*4, 4096)`，512 的兜底只到 **2048**，而实测过的空串悬崖就在
   2000 附近。也就是说旧值连「兜底那一次」都还踩在悬崖里侧。

② **`instinct_graduation` 压根不走 `llm_client`。** 它自己手搓了一份
   `requests.post` + 一份配置读取 + 一个写死的 512，于是那套截断重试对它完全
   不存在。链路是：思考吃光预算 → content 空 → HTTP 仍 200 → 一行日志都不打
   → 上游 `if not distilled: return None` 当成「没什么可毕业的」 → 整条毕业
   静默不发生。**绿灯亮着，活没干。**

所以整改不是把 512 改成别的数字，是：把重复客户端删掉转交 `llm_client`、把预算
收成 `COGNITIVE_MAX_TOKENS` 一个真相源、给空蒸馏补上日志、再加一条射程守卫
防这个形态重新长出来。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_cognitive_budget.py -v
全程不发一个真实请求。
"""
from __future__ import annotations

import ast
import json
import logging
import os
import pathlib
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import ducky.instinct_graduation as graduation  # noqa: E402
import ducky.llm_client as llm_client  # noqa: E402
from ducky.llm_client import COGNITIVE_MAX_TOKENS  # noqa: E402

_ROOT = pathlib.Path(_REPO_ROOT)

# 认知类调用的三处 —— 用户视角审计 A2 点名的那三处，一个不许少
_COGNITIVE_SITES = (
    "ducky/governance.py",
    "ducky/self_edit.py",
    "ducky/instinct_graduation.py",
)


class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


def _truncated_body() -> str:
    """推理截断的实测形态：200 + content 空 + finish_reason=length + reasoning_content"""
    return json.dumps({
        "choices": [{
            "finish_reason": "length",
            "message": {"role": "assistant", "content": "",
                        "reasoning_content": "思考中……"},
        }],
    }, ensure_ascii=False)


def _ok_body(text: str) -> str:
    return json.dumps({"choices": [{"message": {"content": text}}]}, ensure_ascii=False)


@pytest.fixture
def _cfg(monkeypatch):
    monkeypatch.setattr(llm_client, "_config_cache", {
        "model": "test-model",
        "base_url": "http://fake.local/v1",
        "api_key": "fake-key",
    })


class _FakeMem:
    """极简 mem0 替身：只够跑通 graduate_to_skill 一条路径。"""

    def __init__(self, items):
        self.items = items
        self.added: list[dict] = []
        self.deleted: list[str] = []

    def get_all(self, filters=None, limit=10000, **kw):
        return {"results": [dict(i) for i in self.items]}

    def add(self, messages, user_id=None, metadata=None, **kw):
        self.added.append({"messages": messages, "user_id": user_id,
                           "metadata": dict(metadata or {})})
        return {"results": []}

    def delete(self, memory_id):
        self.deleted.append(memory_id)


def _three_instincts():
    """刚好踩在 MIN_GROUP_SIZE 上的一组同类 instinct。"""
    return [{"id": f"m_{i}", "memory": f"打卡记录{i}",
             "metadata": {"category": "打卡", "bank_id": "default"}} for i in range(3)]


# ═══════════════ ① 根因：毕业链现在继承了截断重试 ═══════════════

def test_graduation_survives_reasoning_truncation_and_actually_graduates(_cfg, monkeypatch):
    """★ 核心断言：推理模型把首试预算思考光了，毕业**照样发生**。

    整改前这条路径的形态是：`_call_llm` 自己发一次 POST → 200 但 content 空 →
    `.strip()` 得空串 → 毕业静默 return None。整改后转交 `call_llm`，截断被识别、
    预算 ×4 重试，蒸馏结果拿到手，`memory.add` 真的被调用。

    断言落在**产物**上（added / deleted），不落在「调了几次」上：后者换个实现
    就漂，前者才是用户能感知的那件事 —— 记忆到底毕业了没有。
    """
    budgets = []

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        budgets.append(json["max_tokens"])
        # 首试必截断，重试才给正文 —— 复刻实测形态
        if len(budgets) == 1:
            return _Resp(200, _truncated_body())
        return _Resp(200, _ok_body("蒸馏后的打卡技能"))

    monkeypatch.setattr(llm_client.requests, "post", fake_post)

    mem = _FakeMem(_three_instincts())
    out = graduation.graduate_to_skill(mem, "user_x", {"category": "打卡", "count": 3})

    assert out == "蒸馏后的打卡技能", (
        "推理截断后毕业没能完成 —— 这正是整改前的静默形态：HTTP 200、无日志、"
        "毕业不发生"
    )
    assert len(mem.added) == 1, "蒸馏结果没有落库"
    assert mem.added[0]["metadata"]["level"] == "skill"
    assert set(mem.deleted) == {"m_0", "m_1", "m_2"}, "源记忆没被回收"
    assert budgets == [COGNITIVE_MAX_TOKENS, min(COGNITIVE_MAX_TOKENS * 4, 4096)], (
        f"预算轨迹不对：{budgets}。首试必须取 COGNITIVE_MAX_TOKENS，"
        "重试必须是 llm_client 的 ×4 放大 —— 这两件事都只有走 call_llm 才会发生"
    )


def test_graduation_no_longer_hand_rolls_its_own_http_client(_cfg, monkeypatch, caplog):
    """负向对照（第一层）：把 `llm_client` 那一层掐死，毕业必须**跟着**失灵。

    如果 `instinct_graduation` 还偷偷留着自己那份 `requests.post`，掐 `llm_client`
    对它毫无影响，毕业照样成功 —— 这条断言就是为了让「其实没转交」这件事变红。

    ⚠️ 两个坑，都是变异轮当场踩出来的：

    ① 断言落在**行为**上（没落库、没删源、日志有痕），不落在「异常逃出来」上：
       `graduate_to_skill` 尾部有个 `except Exception` 把一切都埋成一句
       `logger.warning("毕业失败: …")`。第一版写的是 `pytest.raises`，被这个宽捕获
       吃掉 —— fable5 M7 那条「特性级入口的宽捕获」在我自己的负向对照上现了原形，
       另案 P1-8 整改。

    ② **`requests.post` 那一层不能一起掐死。** 第一版顺手把
       `llm_client.requests.post` 也换成了抛异常的桩，结果变异轮里把转交撤销、改回
       手搓 `requests.post` 之后，这条用例**照样是绿的** —— 因为
       `llm_client.requests` 和被测模块 `import requests` 拿到的是**同一个模块对象**，
       打在它上面的补丁两条路径都会命中，于是「走了 llm_client」和「自己发的」
       在断言看来一模一样。判据没有区分力，绿得毫无意义。
       改法：让手搓那条路**成功**（`requests.post` 返回一个正常补全体），只掐
       `call_llm` 这一个转交缝。于是真转交了 → 失败 → 绿；偷偷自己发 → 成功 →
       `out is None` 当场变红。
    """
    SABOTAGE = "llm_client 已被掐死"

    def boom(*a, **k):
        raise RuntimeError(SABOTAGE)

    def happy_post(url, headers=None, json=None, timeout=None, **kw):
        # 故意让「手搓那条路」畅通：只有这样，撤销转交才会被抓住
        return _Resp(200, _ok_body("绕过 llm_client 拿到的结果"))

    monkeypatch.setattr(llm_client, "call_llm", boom)
    monkeypatch.setattr(llm_client.requests, "post", happy_post)

    mem = _FakeMem(_three_instincts())
    with caplog.at_level(logging.WARNING, logger="aiduMEM.graduation"):
        out = graduation.graduate_to_skill(mem, "user_x", {"category": "打卡", "count": 3})

    assert out is None, (
        f"掐死 llm_client 之后毕业竟然还成功了（拿到 {out!r}）—— 说明 "
        "instinct_graduation 还留着自己那份补全客户端，转交没做实"
    )
    assert not mem.added and not mem.deleted, "掐死上游后还动了库"
    assert any(SABOTAGE in r.getMessage() for r in caplog.records), (
        "掐死信号没出现在日志里 —— 那这条负向对照其实没打到 llm_client 上，"
        f"它可能压根没被调用。实际日志：{[r.getMessage() for r in caplog.records]}"
    )


# ═══════════════ ② 空蒸馏不许再无声 ═══════════════

def test_empty_distillation_logs_a_warning_naming_the_category(_cfg, monkeypatch, caplog):
    """★ 空答案必须留痕。整改前这里是一句光秃秃的 `return None`。

    重试之后仍然空（真的顶不住），毕业确实要放弃 —— 但**放弃这件事必须有人知道**。
    铁律 8 那句问话「如果这里真失败了，谁会知道？」在这条路径上原本没有答案。
    """
    def always_truncated(url, headers=None, json=None, timeout=None, **kw):
        return _Resp(200, _truncated_body())

    monkeypatch.setattr(llm_client.requests, "post", always_truncated)

    mem = _FakeMem(_three_instincts())
    with caplog.at_level(logging.WARNING, logger="aiduMEM.graduation"):
        out = graduation.graduate_to_skill(mem, "user_x", {"category": "打卡", "count": 3})

    assert out is None, "顶不住就该放弃 —— 但不许无声"
    assert not mem.added and not mem.deleted, "蒸馏空却动了库，比无声更糟"

    hits = [r for r in caplog.records if "蒸馏返回空" in r.getMessage()]
    assert hits, (
        "空蒸馏一条 warning 都没打 —— 「LLM 回了空」和「本来就没什么可毕业」"
        f"在日志里长得一模一样。实际收到：{[r.getMessage() for r in caplog.records]}"
    )
    msg = hits[0].getMessage()
    assert "打卡" in msg and "3" in msg, (
        f"告警里没点名是哪一组、多少条，运维拿不到可行动信息：{msg}"
    )


# ═══════════════ ③ 预算是一个决定，不是三份抄写 ═══════════════

def test_all_three_cognitive_sites_read_the_one_shared_budget():
    """★ 三处逐一点名（用户视角审计 A2 的验收基准）：语法树里不许再有本地预算字面量。

    判据用 AST 不用字符串：这条断言前后被我自己绊了三次 —— **解释「512 已被删掉」
    的那段注释里也写着 512**，字符串级判据分不清「代码在做这件事」和「注释在说
    这件事已经不做了」。注释和 docstring 天然不进语法树，措辞怎么写都不影响结论。
    """
    for rel in _COGNITIVE_SITES:
        src = (_ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(src)

        # 任何 max_tokens=<数字字面量> 都算私藏预算
        local = []
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "max_tokens" \
                    and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, int):
                local.append((node.lineno, node.value.value))
            if isinstance(node, ast.arg) and node.arg == "max_tokens":
                pass  # 默认值挂在 FunctionDef.args.defaults 上，下面单独查
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [a.arg for a in node.args.args + node.args.kwonlyargs]
                defaults = list(node.args.defaults) + list(node.args.kw_defaults)
                for nm, dv in zip(names[-len(defaults):] if defaults else [], defaults):
                    if nm == "max_tokens" and isinstance(dv, ast.Constant) \
                            and isinstance(dv.value, int):
                        local.append((node.lineno, dv.value))

        assert not local, (
            f"{rel} 里还私藏着输出预算字面量 {local} —— 预算必须取 "
            "llm_client.COGNITIVE_MAX_TOKENS，否则「改预算」又变成三个地方各改一遍"
        )
        assert "COGNITIVE_MAX_TOKENS" in src, (
            f"{rel} 没有引用 COGNITIVE_MAX_TOKENS —— 它是三处认知调用的唯一真相源"
        )


def test_retry_budget_clears_the_measured_empty_string_cliff():
    """★ 为什么是 1024 不是 512：兜底那一次必须**越过**实测悬崖。

    实测过的形态：推理模型 + 2000 上限 → 抽取回空串。`call_llm` 的重试按
    `min(max_tokens*4, 4096)` 放大，于是：
      · 512  → 兜底 2048，**仍在悬崖里侧**，重试等于白跑一趟；
      · 1024 → 兜底 4096，越过。
    这条断言把「1024 是量出来的不是拍出来的」焊在测试里；哪天有人顺手把常量调回
    512 省钱，这里立刻变红并说明理由。
    """
    MEASURED_CLIFF = 2048  # 实测空串悬崖（推理模型，约 2000 附近）
    retry = min(COGNITIVE_MAX_TOKENS * 4, 4096)
    assert retry > MEASURED_CLIFF, (
        f"COGNITIVE_MAX_TOKENS={COGNITIVE_MAX_TOKENS} 的兜底预算只有 {retry}，"
        f"没越过实测悬崖 {MEASURED_CLIFF} —— 重试白跑一趟，等于没有兜底"
    )
    assert min(512 * 4, 4096) <= MEASURED_CLIFF, (
        "负向对照失效：旧值 512 的兜底本应正好卡在悬崖上，若这条不成立，"
        "说明悬崖常数或重试公式变了，上面那条断言的理由也就不成立了"
    )


# ═══════════════ ④ 射程守卫：防这个形态重新长出来 ═══════════════

def test_no_module_hand_rolls_its_own_completion_client():
    """★ 元测试：`ducky/` 下不许再出现第二个手搓的补全客户端。

    P1-5 的根因不是一个数字，是**一份重复实现**：重复实现里那些硬化（截断重试、
    响应体兜底解析、密钥回退）一条都没有，而它长得和正常代码一模一样。
    删掉一次不够，得让它长不回来。

    判据：语法树里对 `requests.post(...)`／`urllib.request.urlopen(...)` 的调用，
    只允许出现在下面这张**按文件名精确豁免**的表里 —— 不许目录级豁免（铁律 12）。

    豁免表下面那半截「不许烂成永真」的检查不是装饰：我第一版顺手把
    `ducky/router_usage.py` 也写进了豁免表（想着「它也往外发东西」），结果被自己
    这半截当场抓出来 —— 它走的是 SSH，根本不发 HTTP。**没量就往豁免表里加条目，
    豁免表就是这样一条条烂成永真的。**
    """
    ALLOWED = {
        "ducky/llm_client.py":
            "认知层唯一的补全通道本体：截断重试、SSE 拼接体兜底解析、"
            "__SF_KEY__ → .llm_key → .sensenova_key 的密钥回退都在这里。",
        "ducky/pipeline/memory_vision.py":
            "多模态：请求体要塞图片，call_llm 只走纯文本，形状上无法转交。"
            "它自己那条链路的硬化程度另案追踪，但它不是认知类文本调用。",
    }

    offenders = {}
    for path in sorted((_ROOT / "ducky").rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        lines = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in ("post", "urlopen"):
                root = f.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name) and root.id in ("requests", "urllib", "httpx"):
                    lines.append(node.lineno)
        if lines and rel not in ALLOWED:
            offenders[rel] = lines

    assert not offenders, (
        "以下模块自己发起了 HTTP 补全/请求调用，却不在精确豁免表里：\n  "
        + "\n  ".join(f"{k}:{v}" for k, v in offenders.items())
        + "\n认知类文本调用一律转交 ducky.llm_client.call_llm（那里才有截断重试、"
          "响应体兜底和密钥回退）。确有例外，请在本用例的 ALLOWED 表里按**文件名**"
          "登记并写明「为什么形状上无法转交」。"
    )

    # 豁免表自身不许烂成永真：登记了却已不发请求的条目要摘掉
    stale = []
    for rel in ALLOWED:
        p = _ROOT / rel
        if not p.exists():
            stale.append(f"{rel}（文件已不存在）")
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        hit = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("post", "urlopen")
            for n in ast.walk(tree)
        )
        if not hit:
            stale.append(f"{rel}（已不再发请求）")
    assert not stale, (
        "豁免表里有陈旧条目，正在烂成永真：\n  " + "\n  ".join(stale)
    )
