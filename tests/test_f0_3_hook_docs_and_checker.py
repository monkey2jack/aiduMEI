"""f0.3 remediation: the install text names the event the checker requires,
and the checker no longer calls a double installation green.

O-4  prompts/install.txt (mirrored by ONE_LINE_INSTALL.md) told agents to hook
     distill on `session_end` and called a missing distill hook harmless, while
     scripts/check_hook_deployment.py requires `on_session_end`.
H-6  memory.provider aidumem plus the aiduMEI shell hooks (double write, double
     distill) was judged green; it is now a yellow warning (exit code 2).
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts/check_hook_deployment.py"
DOCS = ("prompts/install.txt", "ONE_LINE_INSTALL.md", "docs/AGENT_INTEGRATION.md",
        "integrations/INTEGRATION_GUIDE.md")
HARMLESS = "漏挂不致命"      # the f0.2 wording that called the missing hook harmless
NEGATIONS = ("not `", "不是 `")


def _checker():
    spec = importlib.util.spec_from_file_location("_f03_hook_checker", CHECKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# O-4: the documented event is the one the checker requires
# ---------------------------------------------------------------------------

def test_install_prompt_names_on_session_end_and_does_not_call_it_harmless():
    prompt = (ROOT / "prompts/install.txt").read_text(encoding="utf-8")
    assert prompt == (ROOT / "ONE_LINE_INSTALL.md").read_text(encoding="utf-8")
    distill = next(line for line in prompt.splitlines() if "aidumem-distill.sh" in line)
    assert "on_session_end" in distill
    assert not re.search(r"(?<![\w/])session_end\b", distill.replace("on_session_end", ""))
    assert HARMLESS not in prompt
    assert "check_hook_deployment.py" in distill
    assert "on_session_end" in _checker().REQUIRED_EVENTS


@pytest.mark.parametrize("rel", DOCS)
def test_docs_only_use_session_end_as_a_counter_example(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    for m in re.finditer(r"(?<![\w/])session_end\b", text):
        before = text[max(0, m.start() - 8):m.start()]
        assert any(before.endswith(neg) for neg in NEGATIONS), (
            f"{rel}: bare `session_end` near {text[max(0, m.start() - 40):m.end() + 20]!r}")


def test_the_counter_example_check_has_discriminating_power():
    """Control: the f0.2 prompt line is caught by the same two checks."""
    old = ("（Hermes 挂 session_end，脚本 integrations/aidumem-distill.sh）" + HARMLESS)
    assert re.search(r"(?<![\w/])session_end\b", old.replace("on_session_end", ""))
    assert HARMLESS in old


# ---------------------------------------------------------------------------
# H-6: plugin + shell hooks is a yellow double installation
# ---------------------------------------------------------------------------

def _host(tmp_path: Path, *, provider: bool, events=("pre_llm_call", "post_llm_call",
                                                     "on_session_end"), drift: str = "") -> Path:
    sources = {"pre_llm_call": "aidumem-inject.sh", "post_llm_call": "aidumem-ingest.sh",
               "on_session_end": "aidumem-distill.sh"}
    lines = ["memory:", "  provider: aidumem"] if provider else []
    lines.append("hooks:")
    for event in events:
        host = tmp_path / sources[event]
        shutil.copy2(ROOT / "integrations" / sources[event], host)
        if event == drift:
            host.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
        lines += [f"  {event}:", f'    - command: "{host}"']
    config = tmp_path / "config.yaml"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config


def _run(config: Path, *extra: str):
    return subprocess.run([sys.executable, str(CHECKER), "--config", str(config),
                           "--repo", str(ROOT), *extra], capture_output=True, text=True,
                          timeout=30)


def test_provider_plus_all_shell_hooks_is_yellow_not_green(tmp_path):
    config = _host(tmp_path, provider=True)
    machine = _run(config, "--json")
    report = json.loads(machine.stdout)
    assert machine.returncode == 2
    assert report["level"] == "yellow" and report["ok"] is False
    assert report["double_install"] is True
    assert report["duplicated_events"] == ["on_session_end", "post_llm_call", "pre_llm_call"]
    assert all(item["status"] == "ok" for item in report["items"])
    assert any(w.startswith("double_install:") for w in report["warnings"])
    human = _run(config)
    assert human.returncode == 2 and "\U0001f7e1" in human.stdout


@pytest.mark.parametrize("mode", ["hooks", "plugin"])
def test_explicit_integration_mode_does_not_hide_the_double_install(tmp_path, mode):
    report = json.loads(_run(_host(tmp_path, provider=True), "--json",
                             "--integration", mode).stdout)
    assert report["double_install"] is True and report["level"] == "yellow"


def test_same_hooks_without_the_provider_stay_green(tmp_path):
    """Control: yellow comes from the provider, not from the hook files."""
    result = _run(_host(tmp_path, provider=False), "--json")
    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report["level"] == "green" and report["ok"] is True
    assert report["double_install"] is False and report["warnings"] == []


def test_provider_alone_is_still_not_applicable(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("memory:\n  provider: aidumem\n", encoding="utf-8")
    result = _run(config, "--json")
    report = json.loads(result.stdout)
    assert result.returncode == 0 and report["applicability"] == "not_applicable"
    assert report["double_install"] is False and report["level"] == "na"


def test_red_findings_keep_precedence_over_the_yellow_warning(tmp_path):
    result = _run(_host(tmp_path, provider=True, drift="post_llm_call"), "--json")
    report = json.loads(result.stdout)
    assert result.returncode == 1 and report["level"] == "red"
    assert report["double_install"] is True
