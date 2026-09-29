#!/usr/bin/env python3
"""count_test_kinds — README 测试总数的「行为 / 脚本行为 / 守卫」三分口径（v20.5.1 · T-09）

为什么存在：README 头条的用例总数曾是一个混合数——行为测试与「检查文档数字/
品牌字面量」的守卫测试混在一起报，数字越大越好看，但说明不了行为验证强度。
本脚本把总数拆成三桶，每桶都可独立复算：

- **行为用例**：测试函数直接 import 并调用产品代码（ducky / api_server /
  mcp_server / benchmarks / scripts 下的 Python 模块）；
- **脚本/钩子行为**：不 import 产品包，但通过 subprocess/文件系统真实执行
  脚本、钩子、部署产物（行为仍被真执行，只是入口不在包内）；
- **守卫用例**：主判据是仓库文档、口径字面量或目录结构的静态文本
  （下面 _GUARD_FILES 名单，逐项带理由——与仓内其它守卫同一约定：
  名单可以改，理由不许空）。

用法：`python scripts/count_test_kinds.py`；输出三桶计数与总数。
总数应与 `pytest --collect-only -q` 的收集数一致（本脚本内部就是跑它）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# ── 纯守卫名单（主判据 = 仓库文本/结构，不执行任何被测行为）────────────
# 格式：文件名 → 理由。新增文件进名单必须带理由；名单该缩就缩。
_GUARD_FILES: dict[str, str] = {
    "test_env_example_is_complete.py": ".env.example 与 env_registry 的口径一致性",
    "test_f0_2_wiring.py": "f0.2 插件与安装脚本接线的 AST/文本守卫",
    "test_f0_3_token_off_argv.py": "全仓 shell 脚本 token 不进命令行的文本扫描",
    "test_v19_4_2_brand_surface.py": "品牌字面量在公开面的出现位置",
    "test_v19_4_2_hermes_host_resolution.py": "宿主路径解析的文档口径核对",
    "test_v20_1_1_source_guards.py": "源码文本级守卫（脱敏/口径字面量）",
    "test_v20_4_deprecation.py": "弃用登记的文档存在性",
    "test_v20_4_except_ratchet.py": "except 密度棘轮（源码文本计数）",
    "test_v20_4_mcp_auth_doc.py": "MCP 鉴权文档与实现措辞一致性",
    "test_v20_brand_literal_census.py": "品牌字面量普查",
    "test_v20_brand_policy.py": "品牌策略的文本面",
    "test_v20_brand_visible_surface.py": "品牌可见面文本",
    "test_v20_ci_pipeline.py": "CI YAML 结构断言",
    "test_v20_deploy_manifest.py": "发布清单文本",
    "test_v20_dev_deps_declaration.py": "依赖声明文本",
    "test_v20_doc_asset_links.py": "文档本地引用不坏链",
    "test_v20_gitignore_guard.py": ".gitignore 结构断言",
    "test_v20_import_shadowing.py": "同名导入遮蔽的 AST 扫描",
    "test_v20_readme_known_exceptions.py": "README 例外登记",
    "test_v20_runtime_deps_declaration.py": "运行时依赖声明文本",
}

# ── 脚本/钩子行为名单（不 import 产品包，但真执行脚本/钩子/产物）────────
_SCRIPT_BEHAVIOR_FILES: dict[str, str] = {
    "test_f0_2_commit_metadata_scan.py": "合成 Git 仓执行提交身份脱敏门禁的正反行为",
    "test_f0_2_ops_audit.py": "钩子、cron 与升级脚本的实际命令行为",
    "test_f0_2_release_scan_public_policy.py": "完整词表扫描与公开基线策略的命令行为",
    "test_f0_2_shell_distill_idempotency.py": "会话萃取 shell 钩子真实执行",
    "test_f0_2_shell_ingest_receipts.py": "写入 shell 钩子的回执与自检行为",
    "test_f0_2_shell_scope.py": "shell 钩子作用域传递行为",
    "test_f0_3_acceptance_gate.py": "验收脚本真实执行（解释器探测/crontab 对账/全量编译）",
    "test_f0_3_consolidator_logging.py": "consolidator 在 cron 式重定向下的真实子进程日志行为",
    "test_f0_3_container_deploy.py": "容器健康检查命令在无 curl 的 PATH 下真跑",
    "test_f0_3_cron_roots.py": "update_crontab.sh 与清单在假 crontab 上的真实安装行为",
    "test_f0_3_hook_docs_and_checker.py": "安装文档口径与部署自检脚本的真实判定",
    "test_f0_3_identity_gate.py": "合成 Git 仓经真实 pre-push 钩子推送的身份门禁",
    "test_f0_3_inject_circuit.py": "读钩子熔断/探活/凭据传递的真实执行（多 locale）",
    "test_f0_3_restore_tombstones_script.py": "墓碑批量恢复脚本对假 HTTP 服务的真实执行",
    "test_hermes_plugin.py": "宿主插件契约（真实调用宿主基类签名核对）",
    "test_inject_hook.py": "注入钩子脚本行为",
    "test_memory_gate_entities.py": "实体门槛脚本行为",
    "test_v19_4_1_backup_gate.py": "备份闸门脚本真实执行",
    "test_v19_4_2_brand_surface.py": None,  # 占位防御：同名若在守卫表则不计入
    "test_v19_4_inject_frame.py": "注入框架边界编码的真实行为",
    "test_v20_3_1_drill_autoshift.py": "autoshift 演练脚本",
    "test_v20_4_model_hash.py": "模型哈希清单校验脚本",
    "test_v20_lifespan_background.py": "生命周期后台行为",
    "test_v20_p38_least_privilege.py": "最小权限面的脚本级核对",
    "test_v20_subprocess_env_isolation.py": "子进程环境隔离的真实进程行为",
    "test_v20_upgrade_gate.py": "升级闸门脚本",
}
_SCRIPT_BEHAVIOR_FILES = {k: v for k, v in _SCRIPT_BEHAVIOR_FILES.items() if v}


def _collect_counts() -> Counter:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
        cwd=_ROOT, capture_output=True, text=True, timeout=300,
    )
    counts: Counter = Counter()
    for line in out.stdout.splitlines():
        m = re.match(r"(tests/[^\s:]+?\.py)::", line)
        if m:
            counts[Path(m.group(1)).name] += 1
    return counts


def main() -> int:
    counts = _collect_counts()
    if not counts:
        print("❌ 收集为空 —— pytest collect 失败，先看它的输出", file=sys.stderr)
        return 2
    guard = sum(counts[f] for f in _GUARD_FILES if f in counts)
    script = sum(counts[f] for f in _SCRIPT_BEHAVIOR_FILES if f in counts)
    total = sum(counts.values())
    behavior = total - guard - script
    print(f"用例总数（pytest --collect-only 实测）: {total}")
    print(f"  行为用例（产品代码直测）: {behavior}")
    print(f"  脚本/钩子行为用例: {script}")
    print(f"  守卫用例（文档/口径/结构）: {guard}")
    unknown = sorted(set(counts) - set(_GUARD_FILES) - set(_SCRIPT_BEHAVIOR_FILES))
    # 不在任何名单里的文件默认归入行为桶 —— 它们 import 了产品代码。
    # 若其中有纯守卫，应把它移入 _GUARD_FILES 并写明理由。
    print(f"（默认入行为桶的文件数: {len(unknown)}；归错桶的请挪名单并写理由）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
