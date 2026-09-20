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


_BASELINE = 688  # v22.0 雷霆审计 B1：+2 —— health.py 非 _ok 键降级探针的兜底。
# routes_v8.py 里 30 多个路由一律是 `except Exception: return {"status":"error"}`，
# 路由层的契约就是「任何异常都变成 JSON，不让 500 裸奔」；单独给这一个收窄，
# 换来的是漏网异常在这条路上变成 500，与同文件其余路由行为不一致。
# 模块自身（ducky/session_distill.py）零宽捕获，四处都按错误形态收窄了。
# v21.2.0 写入活性探针：+3（探针自身读两个库的容错 +
# 自查脚本脱离仓库单跑时的凭据兜底 —— 一个「告诉你有没有在写」的探针
# 绝不能自己把 /health 打炸；底层 sqlite/import 错误形态各异收窄不掉）；
# 原 v21.2.0 审计整改轮：+3（两个新探针的读库容错 +
# episode rollup 的降级捕获——rollup 会改变返回条数，失败必须留 warning
# 而不是静默不聚合）；原 +2 记为（/health 新增 epistemic_session_coverage 与
# episode_ok 两个探针的读库容错 —— 探针本身绝不许把 /health 打炸，
# 底层 sqlite/import 错误形态各异收窄不掉，如实抬基线）；v21.2 Memmy 融改：+19（M1 episode 轨迹写入与报表容错、layer1 打标缝位的轨迹登记降级、
# M2 回声抑制查询与 sidecar 列探测降级、M4/M6 配置 fail-closed、M7 rollup 降级、
# M8 借阅留痕降级）——全部是「统计/留痕失败绝不许打炸主链路」这一类，
# 收窄异常类型做不到（底层 sqlite/import/属性错误形态各异），故如实抬基线；
# v21.0 收口：+4 schema v6 迁移容错 +1 §16 钩子 +9 dossier 只读容错 +2 dossier 域拆分/sidecar 容错 +1 scoring sidecar 批量加载 +2 add.py 主链路打标降级钩子；v21.1 众神殿：+9 routes_pantheon 端点容错 + dossier 借阅 403 + 建表/借阅门降级
# 2026-09-10 v20.5.0 正式版：+5 均用户审计整改的「降级钩子/迁移容错」——
# wal_engine.py×2（DELETE 终链同事务留痕 ×2 路径，失败不拖垮删除主路径）、
# refine_memory.py×1（回滚终链同型）、memory_lineage.py×1（UNIQUE 索引存量
# 兼容失败 warning 出声而非炸启动）、schema_bootstrap.py×1（v5 回填失败不
# 阻塞启动、verify 如实报出）。全部为「失败只降级不阻断 + 有日志」同型惯例。
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
