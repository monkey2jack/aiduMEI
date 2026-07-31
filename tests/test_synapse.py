import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ducky.core_memory import init_core_memory, get_all_blocks, put_block, inject_context as cm_inject
from ducky.checkpoint import write_checkpoint, get_latest_checkpoint, inject_context as cp_inject
from ducky.autodream import trigger_dream, get_dream_status, get_dream_report

def run_tests():
    print("🤖 启动 Synapse 模块单元测试...")
    
    # 1. CoreMemory 测试
    init_core_memory()
    blocks = get_all_blocks()
    assert "core_user_profile" in blocks, "缺失 core_user_profile"
    assert "core_current_project" in blocks, "缺失 core_current_project"
    assert "core_key_decisions" in blocks, "缺失 core_key_decisions"
    print("  - [PASS] CoreMemory 初始化与读取")

    # 更新 block 测试
    put_block("core_key_decisions", "①重启网关必须先通知user；②响应徽章已彻底关闭；③重新优化完成")
    updated = get_all_blocks()
    assert "重新优化完成" in updated["core_key_decisions"]["content"], "更新未生效"
    print("  - [PASS] CoreMemory 修改与内容断言")

    cm_ctx = cm_inject()
    assert "[CoreMemory · Clotho]" in cm_ctx, "CoreMemory 注入格式错误"
    print("  - [PASS] CoreMemory 上下文注入生成")

    # 2. Checkpoint 测试
    cp_data = {
        "cp_active_intent": "测试 Synapse 功能",
        "cp_next_action": "跑单元测试脚本",
        "cp_current_work": "重构与校验",
        "cp_key_decisions": "保证高健壮性",
        "cp_open_notes": "没有待办了"
    }
    write_checkpoint("test-session-xyz", cp_data)
    latest = get_latest_checkpoint()
    assert latest is not None, "最新快照为空"
    assert latest["session_id"] == "test-session-xyz", "session_id 匹配错误"
    assert latest["blocks"]["cp_active_intent"] == "测试 Synapse 功能", "快照内容错误"
    print("  - [PASS] Checkpoint 写入与检索")

    cp_ctx = cp_inject()
    assert "[Checkpoint · 上次会话]" in cp_ctx, "Checkpoint 注入格式错误"
    print("  - [PASS] Checkpoint 上下文注入生成")

    # 3. AutoDream 测试
    status = get_dream_status()
    assert status["status"] in ["ready", "never_run"], "状态异常"
    print("  - [PASS] AutoDream 状态获取")

    report = trigger_dream()
    # 无论 skipped 还是 completed，均应为 dict 且包含 status 键
    assert "status" in report, "报告缺少 status"
    print(f"  - [PASS] AutoDream 蒸馏触发成功，状态: {report['status']}")
    
    print("\n🎉 所有新增模块单元测试 100% 通过！")

if __name__ == "__main__":
    run_tests()
