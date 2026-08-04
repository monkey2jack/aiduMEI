#!/usr/bin/env python3
"""aidumem-inject.sh 的 payload 形状兼容性测试。

覆盖三种 payload 形状 + 边界条件，确保 hook 不会再因字段名错位而静默失效。
运行：python3 tests/test_inject_hook.py
"""
import json
import os
import subprocess
import sys

HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "integrations",
    "aidumem-inject.sh",
)

PASS, FAIL = [], []


def run(payload: dict, env_extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.setdefault("AIDUMEM_USER_ID", "default")
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["bash", HOOK],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, f"hook 必须永远 exit 0，实际 {proc.returncode}"
    out = proc.stdout.strip()
    assert out, "hook 必须永远输出 JSON"
    return json.loads(out)


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}{(' — ' + detail) if detail else ''}")


def hist(n: int) -> list:
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(n)]


def main() -> int:
    long_msg = "上次说的那个部署方案还记得吗"

    # 1. v0.20 真实形状：extra.conversation_history + extra.user_message
    r = run({
        "hook_event_name": "pre_llm_call",
        "session_id": "s1",
        "cwd": "/root",
        "extra": {"user_message": long_msg, "conversation_history": hist(12)},
    })
    check("v0.20 形状（extra.conversation_history）触发注入", bool(r.get("context")),
          f"context {len(r.get('context',''))} chars")

    # 2. 顶层形状（旧 Hermes / 第三方调用方）
    r = run({"user_message": long_msg, "conversation_history": hist(12)})
    check("顶层 conversation_history 形状兼容", bool(r.get("context")))

    # 3. 远古形状：messages
    r = run({"user_message": long_msg, "messages": hist(12)})
    check("旧 messages 字段仍兼容", bool(r.get("context")))

    # 4. 短会话必须静默（省 token）
    r = run({"extra": {"user_message": long_msg, "conversation_history": hist(2)}})
    check("短会话（2 条）静默", r == {}, json.dumps(r, ensure_ascii=False)[:60])

    # 5. 短消息必须静默
    r = run({"extra": {"user_message": "嗯", "conversation_history": hist(12)}})
    check("短消息（<3 字符）静默", r == {})

    # 6. 空 payload 不许崩
    r = run({})
    check("空 payload 安全降级", r == {})

    # 7. 畸形 payload 不许崩（非法 JSON 走 stdin）
    proc = subprocess.run(
        ["bash", HOOK], input="not-json-at-all",
        capture_output=True, text=True, timeout=30,
    )
    check("畸形 stdin 安全降级", proc.returncode == 0 and proc.stdout.strip() == "{}")

    # 8. 服务不可达时不许阻塞 LLM 调用
    r = run(
        {"extra": {"user_message": long_msg, "conversation_history": hist(12)}},
        {"AIDUMEM_URL": "http://127.0.0.1:9", "AIDUMEM_TIMEOUT": "1.0"},
    )
    check("服务不可达时静默降级", r == {})

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
