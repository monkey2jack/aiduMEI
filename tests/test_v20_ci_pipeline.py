"""tests/test_v20_ci_pipeline.py — v20 P2-1：测试纪律必须由机器执行

k3 高-2 ＝ fable5 H2，两份独立审计罕见地完全同调，都把这条评为**全报告性价比最高**：

    `.github/workflows/` 曾只有 `docker.yml`；全 `.github/` 搜 `pytest` = 0 命中。
    78 个测试文件不在 CI 跑 = 测试纪律靠人肉。
    现在 aiduMEI 采用 GitHub 源码/Release 分发，不再发布 GHCR；本文件同时守住
    「有发布 job 必须先测」和「明确无发布 job 时不能悄悄长回来」两种状态。

这个仓最大的资产就是测试纪律。而在这条整改之前，那份资产的全部执行力来自
「有人记得跑一遍」。执行人状态一波动，防线整体失效。

所以这个文件守的不是「有没有一个 yml 文件」，是**「从一次 push 到一个已发布镜像，
存不存在一条绕过测试的路径」**。判据按结构走（哪个 job `needs` 哪个 job），
不按关键词走 —— 一个写着 pytest 三个字但没人依赖它的 job，是这条缺陷最体面的
复现形态。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_ci_pipeline.py -v
"""
from __future__ import annotations

import os
import pathlib
import shlex
import sys

import pytest
import yaml

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_ROOT = pathlib.Path(_REPO_ROOT)
_WF_DIR = _ROOT / ".github" / "workflows"
_POLICY_MARKER = "distribution-policy: github-source-only"

#: 「这个 job 会把东西发出去」的信号。发布动作一旦落地就收不回来，
#: 所以它们全都必须挂在测试后面。
_PUBLISH_MARKERS = (
    "docker/build-push-action",
    "docker/login-action",
    "pypa/gh-action-pypi-publish",
    "twine upload",
    "softprops/action-gh-release",
    "actions/create-release",
    "gh release create",
)


def _workflows() -> dict[str, dict]:
    assert _WF_DIR.is_dir(), f"{_WF_DIR} 不存在 —— CI 目录都没有，无从谈起"
    out = {}
    for p in sorted(_WF_DIR.glob("*.y*ml")):
        out[p.name] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    assert out, "workflows 目录是空的"
    return out


def _job_text(job: dict) -> str:
    """把一个 job 压成可搜索的文本（含 steps 的 uses/run）。"""
    return yaml.safe_dump(job, allow_unicode=True)


def _safe_tokens(line: str) -> list:
    """把一行 shell 切成 token，**不许因为合法写法而崩**。

    v20.2.4：`shlex.split` 遇到行尾续行（`... \\`）会抛
    `ValueError: No escaped character`，而续行是完全合法的 shell。守卫崩掉是
    ERROR 而不是 FAIL —— 看起来像「守卫坏了」而不是「代码有问题」，是最容易
    被耸肩放过的一种失败。

    退路选「粗一点的切分」而不是「跳过这一行」：万一跳掉的正是 pytest 那一行，
    这条守卫就会报「CI 里没人跑测试」——一个假红灯。
    """
    if not line.strip():
        return []
    try:
        return shlex.split(line, comments=True)
    except ValueError:
        return line.replace("\\", " ").split()


def _runs_pytest(job: dict) -> bool:
    for step in job.get("steps") or []:
        if "pytest" in str(step.get("run") or ""):
            return True
    return False


def _needs(job: dict) -> list[str]:
    n = job.get("needs")
    if n is None:
        return []
    return [n] if isinstance(n, str) else list(n)


def _source_only_policy() -> bool:
    """明确的源码分发策略才允许仓库没有发布 job。"""
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    return _POLICY_MARKER in readme


# ═══════════════ ① 流水线本体存在，而且真的跑 pytest ═══════════════

def test_a_workflow_actually_runs_the_test_suite():
    """★ 最基本的一条：CI 里必须有一个 job 真的执行 `pytest tests/`。

    判据落在 step 的 `run` 上，不落在文件名或 job 名上：一个叫 `pytest` 却什么都
    不跑的 job，正是这条缺陷最体面的复现形态。
    """
    runners = []
    for name, wf in _workflows().items():
        for job_id, job in (wf.get("jobs") or {}).items():
            if _runs_pytest(job):
                runners.append(f"{name}:{job_id}")
    assert runners, (
        "没有任何 CI job 执行 pytest —— 78 个测试文件全靠人肉跑（k3 高-2 / fable5 H2）"
    )

    # 它必须覆盖**整个** tests/ 目录，不许只挑几个文件跑。
    #
    # ⚠️ 判据按 shell token 走，不按子串走。第一版写的是 `"pytest tests/" in run`，
    # 而 `"pytest tests/" in "pytest tests/test_v20_ci_pipeline.py -q"` **也成立**
    # —— 前者是后者的前缀。也就是说「只挑一个文件跑」这个变异当时压根检测不出来，
    # 是变异轮把它翻出来的：一条本该咬住射程的断言，自己的射程漏了个洞。
    covered = False
    for name, wf in _workflows().items():
        for job in (wf.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                for line in str(step.get("run") or "").splitlines():
                    toks = _safe_tokens(line)
                    if not toks:
                        continue
                    # 允许 `pytest …` 与 `python -m pytest …` 两种写法
                    if "pytest" not in toks:
                        continue
                    targets = [t for t in toks[toks.index("pytest") + 1:]
                               if not t.startswith("-")]
                    if any(t.rstrip("/") == "tests" for t in targets):
                        covered = True
    assert covered, (
        "CI 跑了 pytest，但没有一条命令把整个 tests/ 目录当作靶子 —— "
        "挑着跑等于挑着守。（判据按 token 比对：`pytest tests/some_file.py` 不算）"
    )


def test_test_workflow_is_reusable_and_triggers_on_push_and_pr():
    """★ 触发面必须与**当前交付策略**逐字对齐：只留手动 + 可复用两个面。

    v20.0.1-pre 噪音治理（2026-08-25，SOP 铁律 16）：对外分发面收缩后，
    test.yml 经维护者授权改为「不随 main 推送自动运行」—— 只保留
    `workflow_dispatch`（手动）与 `workflow_call`（供发布流水线复用）。
    本守卫原先断言 push/pull_request 必须在，与已定策略相反，
    改成双向钉死：该在的少一个红，不该在的多一个也红 ——
    有人悄悄把自动触发加回来，失败邮件就会重新开始骚扰维护者。

    `workflow_call` 仍不是可选项：`needs:` 只在同一个工作流内生效，
    发布必须**依赖**测试，而依赖只能靠复用建立。

    用例名保持原样不改：外部审计报告按这个名字引用过它，改名会让
    历史引用变成死链；策略变更的事实记录在本 docstring 与 CHANGELOG。
    """
    wf = _workflows().get("test.yml")
    assert wf, f"缺少 .github/workflows/test.yml，现有：{sorted(_workflows())}"
    on = wf.get("on") or wf.get(True)  # YAML 会把裸 on 解析成布尔 True
    assert isinstance(on, (dict, list)), f"test.yml 的 on: 段形状不对：{on!r}"
    triggers = set(on) if isinstance(on, dict) else set(on)
    assert triggers == {"workflow_dispatch", "workflow_call"}, (
        f"test.yml 触发面与噪音治理策略不符：现有 {sorted(triggers)}，"
        "应恰为 {workflow_dispatch, workflow_call}。"
        "多出 push/pull_request = 自动触发复活（Actions 邮件噪音回归）；"
        "缺 workflow_call = 发布流水线无法依赖测试；缺 workflow_dispatch = 手动跑不了"
    )


def test_ci_pins_the_host_discovery_axis_so_skips_are_reproducible():
    """★ k3 的配套第 2 条：CI 必须显式关掉宿主自动发现。

    不写这一句，「CI 机器上有没有宿主源码」就是一个隐式变量，跳过条数随之飘忽 ——
    而 README 里那几个数字是按轴对得上号的。跳过行为必须**双向可复现**。
    """
    wf = _workflows()["test.yml"]
    job = (wf.get("jobs") or {}).get("pytest") or {}
    env = {**(wf.get("env") or {}), **(job.get("env") or {})}
    for step in job.get("steps") or []:
        env.update(step.get("env") or {})
    assert "HERMES_SRC" in env, (
        f"CI 没有显式设置 HERMES_SRC —— 跳过行为不可复现。现有 env：{sorted(env)}"
    )
    assert str(env["HERMES_SRC"]).lower() in ("off", "none", ""), (
        f"HERMES_SRC={env['HERMES_SRC']!r} 会让 CI 去找宿主源码，跳过条数不再确定"
    )


# ═══════════════ ② 核心：没有一条绕过测试的发布路径 ═══════════════

def test_no_publishing_job_can_run_without_the_tests_passing():
    """★ 这条是整个文件存在的理由：**发布必须挂在测试后面**。

    做法不是「检查 docker.yml 里有没有 pytest 三个字」，而是：
      ① 找出所有会「把东西发出去」的 job（推镜像／发包／发 Release）；
      ② 顺着 `needs` 图往上走，看能不能走到一个真的跑 pytest 的 job；
      ③ 走不到 → 红，并点名是哪个 job。

    顺着依赖图走而不是看同一个文件里有什么，是因为发布 job 可以被加进**任何**
    工作流。判据必须覆盖「以后新增的那一个」，不能只覆盖今天这一个。
    """
    problems = []
    for name, wf in _workflows().items():
        jobs = wf.get("jobs") or {}
        for job_id, job in jobs.items():
            text = _job_text(job)
            if not any(m in text for m in _PUBLISH_MARKERS):
                continue

            # 顺着 needs 图广搜：能否抵达一个真的跑 pytest 的 job
            seen, frontier, guarded = set(), list(_needs(job)), False
            while frontier:
                nxt = frontier.pop()
                if nxt in seen:
                    continue
                seen.add(nxt)
                dep = jobs.get(nxt) or {}
                # 依赖可能是一个「复用工作流」的调用：跟进那个文件
                used = dep.get("uses")
                if used:
                    target = str(used).split("@")[0].lstrip("./")
                    sub = _workflows().get(pathlib.Path(target).name)
                    if sub and any(_runs_pytest(j) for j in (sub.get("jobs") or {}).values()):
                        guarded = True
                        break
                if _runs_pytest(dep):
                    guarded = True
                    break
                frontier.extend(_needs(dep))

            if not guarded:
                problems.append(f"{name}:{job_id}（needs={_needs(job) or '无'}）")

    assert not problems, (
        "以下发布 job 不依赖任何跑过测试的 job —— 一个没跑过测试的提交可以直接"
        "变成已发布产物：\n  " + "\n  ".join(problems)
        + "\n修法：让它 needs: 一个调用 ./.github/workflows/test.yml 的 job。"
    )


def test_the_publish_guard_is_actually_looking_at_a_publishing_job():
    """★ 正向对照：上一条守卫必须确实找到了发布 job，否则它守的是空气。

    「没有任何发布 job 缺少测试依赖」这句话，在**一个发布 job 都没识别出来**时
    也成立 —— 那就是一条永真的绿灯。这里把「至少识别出一个」单独断言出来。
    """
    found = []
    for name, wf in _workflows().items():
        for job_id, job in (wf.get("jobs") or {}).items():
            if any(m in _job_text(job) for m in _PUBLISH_MARKERS):
                found.append(f"{name}:{job_id}")
    if not found:
        # 源码-only 是一种有意的发布策略，不是守卫失效；README 的显式标记
        # 防止未来有人删掉发布 job 后又误以为 CI 仍然保护着某条发布路径。
        assert _source_only_policy(), (
            "没有识别到发布 job，但 README 没有声明 github-source-only 策略；"
            f"请检查 _PUBLISH_MARKERS 是否过时：{_PUBLISH_MARKERS}"
        )
        return


def test_docker_publish_job_specifically_needs_the_tests():
    """点名断言：`docker.yml` 的推镜像 job 必须 `needs` 一个测试 job。

    上面那条是按图搜索的通用判据；这条是对**今天已知的那一个**发布口点名 ——
    通用判据万一哪天被改宽，这条还在原地咬着。
    """
    wf = _workflows().get("docker.yml")
    if not wf:
        assert _source_only_policy(), (
            "docker.yml 不见了，但 README 没有声明 github-source-only 策略"
        )
        return
    jobs = wf.get("jobs") or {}
    push_jobs = [jid for jid, j in jobs.items()
                 if "docker/build-push-action" in _job_text(j)]
    assert push_jobs, f"docker.yml 里找不到推镜像的 job，现有：{sorted(jobs)}"
    for jid in push_jobs:
        deps = _needs(jobs[jid])
        assert deps, f"docker.yml:{jid} 没有任何 needs —— 它可以在测试之前就把镜像推出去"
        reuses_tests = any(
            "test.yml" in str((jobs.get(d) or {}).get("uses") or "") for d in deps
        )
        assert reuses_tests, (
            f"docker.yml:{jid} 的 needs={deps} 里没有一个是调用 test.yml 的 job —— "
            "「先绿后发」必须是 GitHub 的调度保证，不是一句口头约定"
        )
