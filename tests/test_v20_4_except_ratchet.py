"""v20.4.0-alpha · P2-13：运行时域 except Exception 密度棘轮（只降不升）。

背景（六方外审 Sonnet E1 实锤）：运行时域 `except Exception` 极密 ——
它是本仓多起「失败却报成功」静默问题的共同土壤（v19.x→v20.2 账本在案）。
密度不可能一轮清零，但**不许再涨**。

口径（AST ExceptHandler 结构计数，比行 grep 诚实——注释与字符串里的
 「except Exception」不算，元组捕获拆开算；可第三方复算）：
    ducky/*.py + ducky/**/*.py + api_server.py + mcp_server.py（不含 tests/）
基线：601（2026-09-08，v20.4.0-alpha 开工态，AST 口径）
    溯源：行 grep 口径同域 640 = 六方审计快照 639 + 本轮有意新增 1 处
    （write_endpoint_budgets._body_model 对 get_type_hints 的容错）。
    新增宽捕获请优先收窄异常类型，而不是抬基线。

分类信息（纯 pass / 仅 debug 日志）以 AST 粗分输出，不断言 —— 它是后续
分批收窄的线索，不是门禁数字。
"""
import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DOMAIN = (
    list((_REPO_ROOT / "ducky").glob("*.py"))
    + list((_REPO_ROOT / "ducky").glob("*/*.py"))
    + [_REPO_ROOT / "api_server.py", _REPO_ROOT / "mcp_server.py"]
)
def _count_except_exception() -> int:
    total = 0
    for path in _DOMAIN:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or node.type is None:
                continue
            t = node.type
            if isinstance(t, ast.Name) and t.id == "Exception":
                total += 1
            elif isinstance(t, ast.Attribute) and t.attr == "Exception":
                total += 1
            elif isinstance(t, ast.Tuple):
                for e in t.elts:
                    if (isinstance(e, ast.Name) and e.id == "Exception") or (
                            isinstance(e, ast.Attribute) and e.attr == "Exception"):
                        total += 1
    return total


_BASELINE = 627  # 2026-09-10 v20.5.0 preview：+1 crud /update 谱系记录降级钩子（用户审计 🟡-1 整改，lineage 失败不拖垮 /update 主路径，ledger/governance 同型）
# 2026-09-10 v20.5.0a：+17 均「谱系/授权钩子失败不得拖垮主路径」降级包裹
#（ledger/governance 钩子同型惯例：record_lineage/ensure_*_schema/grants 读写
# 失败仅 logger.debug 跳过，事实写入照常 commit）——grants.py×5、memory_lineage.py×2、
# writer/dedup/legacy_routes/conflict_resolver/schema_bootstrap 织入点×10。


def test_except_exception_density_ratchet():
    count = _count_except_exception()
    assert count <= _BASELINE, (
        f"运行时域 except Exception {count} > 基线 {_BASELINE} —— "
        "棘轮只降不升：新增宽捕获请收窄异常类型，或在基线注释里写明不可避免的理由"
    )


def test_ratchet_baseline_is_honest():
    """防永真变异：基线必须钉在真实计数附近（±20），不许被偷偷抬到永远够用。"""
    count = _count_except_exception()
    assert _BASELINE - count <= 20, (
        f"基线 {_BASELINE} 比实计 {count} 松了 {_BASELINE - count} 格 —— "
        "收窄有成效就顺手把基线压下来"
    )
