#!/usr/bin/env python3
"""aiduMEI × Claude Code —— `Stop` 钩子（**写线**）

这一轮对话结束时，把它写回 aiduMEI。

为什么单独有这个文件（v21.2.0 · 一次真实事故的产物）
-----------------------------------------------------
同目录的 `claude-code-hook.py` 是 `pre_compact` 场景的手动工具（把代码塞进
Raw Drawer），它**不是**逐轮写入。在此版本之前，本仓对 Claude Code 只提供
读侧物料，写侧一个都没有——而文档里又写着「把写入挂在 Stop 上」，等于
指向空气。同一个形态在 Hermes 那边酿成过真实事故：读线挂了、写线从没挂过，
对话照常、检索照常、`/health` 全绿，唯独新记忆一条没进，几周后人工审计
翻数据库才发现。

安装
----
`~/.claude/settings.json`（或项目内 `.claude/settings.json`）：

```json
{
  "hooks": {
    "Stop": [
      {"hooks": [{"type": "command",
                  "command": "python3 ~/.claude/hooks/claude-code-stop-hook.py"}]}
    ]
  }
}
```

验收（装完必做，第三步不能省）：

```bash
python3 claude-code-stop-hook.py --selftest   # 真写一条再回读
# ...真聊 5 轮，然后：
python3 scripts/check_ingest_wiring.py        # 退出码非 0 即宿主根本没在调它
```

前两步只证明「脚本能跑」，只有第三步证明「宿主真的在调它」。那次事故里，
脚本一直是好的，没被挂上而已。

设计原则（与 Hermes 侧写线同源）
--------------------------------
- 绝不拖累对话：任何异常都 exit 0，`Stop` 钩子非 0 退出会打断宿主。
- 安静≠失声：失败往 stderr 写一行结构化诊断，静默失败是这次事故的载体。
- 必带 `_origin_session_id` / `_origin_turn`：服务端靠它做回声抑制与轨迹
  信用。注意 session **不是**记忆的作用域——`/clear` 换了 session 照样
  搜得到旧记忆，检索是全库的；不传不会报错，只会让这两个能力静默失效。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

def _num_env(key: str, default: float, *, cast=float, minimum: float = 0.0):
    """读数值型 env，非法值回落默认而不是抛。

    独立集成件 import 不到 ducky.env_config，所以这里自带一份同语义实现。
    裸 int()/float() 在这里尤其致命：解析发生在 import 期，一个手滑的
    `AIDUMEI_INGEST_TIMEOUT=6s` 会让钩子每轮启动即崩 —— 而钩子崩掉的表现
    恰好就是本文件要防的那件事：一声不吭地不写。
    """
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return cast(default)
    try:
        val = cast(raw)
    except (TypeError, ValueError):
        return cast(default)
    if val != val or val in (float("inf"), float("-inf")):   # NaN / inf 先拦
        return cast(default)
    return val if val >= minimum else cast(default)


TIMEOUT = _num_env("AIDUMEI_INGEST_TIMEOUT", 6.0, cast=float, minimum=0.1)
MIN_CHARS = _num_env("AIDUMEI_INGEST_MIN_CHARS", 8, cast=int, minimum=0)
# 服务端 AddRequest 的硬约束是单条 50_000 字符 / 整体 64 KiB；超了是 422，
# 而 422 在这里只会变成一行 stderr，也就是「长对话静默不写」。先裁，理由写明。
LIMIT = 20_000


def _base() -> str:
    return (os.environ.get("AIDUMEM_URL") or "http://127.0.0.1:8767").rstrip("/")


def _read_env_key(path: str, key: str) -> str:
    """从 .env 取一个键。容忍 `export ` 前缀、引号、CRLF、# 注释。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip().lstrip("﻿")
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() != key:
                    continue
                v = v.strip().rstrip("\r")
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if v:
                    return v
    except OSError:
        pass
    return ""


def _lookup(key: str) -> str:
    """环境变量优先，然后走与其它接入物料同一条 .env 链。

    **故意与读侧共用同一条链**：两条线必须解析出同一个租户和同一份凭据，
    否则会出现「写进了 A、读的是 B」——两边各自看着正常，合起来是失忆。
    """
    val = os.environ.get(key)
    if val:
        return val
    for cand in (os.environ.get("AIDUMEM_ENV_FILE") or "",
                 os.path.join(os.environ["AIDUMEM_HOME"], ".env")
                 if os.environ.get("AIDUMEM_HOME") else "",
                 os.path.expanduser("~/.aidumem/.env"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
                 ".env"):
        if cand and os.path.isfile(cand):
            got = _read_env_key(cand, key)
            if got:
                return got
    return ""


def _user_id() -> str:
    return _lookup("AIDUMEM_USER_ID") or _lookup("AIDUMEM_DEFAULT_USER_ID") or "default"


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    tok = (_lookup("AIDUMEM_API_TOKEN") or "").strip()
    if tok:
        h["Authorization"] = "Bearer " + tok
    return h


def _call(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        _base() + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _diag(line: str) -> None:
    if not os.environ.get("AIDUMEM_HOOK_QUIET"):
        sys.stderr.write(line)


def _last_turn(transcript_path: str) -> tuple[str, str]:
    """从 Claude Code 的 JSONL 转录里取最后一轮的 user / assistant 文本。

    形状按「最后一条 user 之后的最后一条 assistant」取，而不是按行号倒数——
    工具循环会在两者之间插入任意多条记录，倒数第 N 行是靠不住的。
    """
    user_text, asst_text = "", ""
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            rows = [ln for ln in fh if ln.strip()]
    except OSError as exc:
        _diag("[aidumem-stop] transcript unreadable err=%s\n" % type(exc).__name__)
        return "", ""

    def _text_of(msg: object) -> str:
        if isinstance(msg, str):
            return msg
        if isinstance(msg, list):
            return "\n".join(
                b.get("text", "") for b in msg
                if isinstance(b, dict) and b.get("type") == "text")
        return ""

    for line in reversed(rows):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        msg = rec.get("message") or {}
        role = rec.get("type") or msg.get("role")
        content = _text_of(msg.get("content"))
        if not content.strip():
            continue
        if role == "assistant" and not asst_text:
            asst_text = content
        elif role == "user" and not user_text:
            user_text = content
            break            # user 之前的都是上一轮，停
    return user_text, asst_text


def _write(user_text: str, asst_text: str, session_id: str, turn: int) -> bool:
    try:
        _call("/add", {
            "messages": [{"role": "user", "content": user_text[:LIMIT]},
                         {"role": "assistant", "content": asst_text[:LIMIT]}],
            "user_id": _user_id(),
            # 同 aidumem-ingest.sh：同步 /add 要跑完整抽取管线（生产实测 p50
            # 约 4 秒），而 Stop 钩子卡住会让宿主等着。异步下服务端先收下。
            "async_mode": True,
            "metadata": {"_origin_agent": "claude-code",
                         "_origin_session_id": session_id[:256],
                         "_origin_turn": turn,
                         "channel": "claude-code-stop"},
        })
        return True
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            _diag("[aidumem-stop] auth_failed status=%d token=%s "
                  "(本轮对话没有写进记忆库；请跑 --selftest)\n"
                  % (exc.code, "present" if "Authorization" in _headers() else "MISSING"))
        else:
            _diag("[aidumem-stop] http_error status=%d (本轮对话没有写进记忆库)\n"
                  % exc.code)
    except Exception as exc:                       # noqa: BLE001 —— 见文件头「绝不拖累对话」
        _diag("[aidumem-stop] unreachable err=%s (本轮对话没有写进记忆库)\n"
              % type(exc).__name__)
    return False


def _selftest() -> int:
    marker = "aidumem stop hook selftest %d" % int(time.time())
    sid = "stop-selftest-%d" % int(time.time())
    uid = _user_id()
    try:
        _call("/add", {"messages": [{"role": "user", "content": marker}],
                       "user_id": uid, "async_mode": True,
                       "metadata": {"_origin_agent": "claude-code-stop-selftest",
                                    "_origin_session_id": sid, "_origin_turn": 1,
                                    "channel": "selftest"}})
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            sys.stderr.write(
                "[aidumem-stop] selftest FAILED auth_failed status=%d url=%s user_id=%s\n"
                "  → 门禁已开启但钩子没拿到有效 token；每一轮都会静默不写。\n"
                % (exc.code, _base(), uid))
            return 3
        sys.stderr.write("[aidumem-stop] selftest FAILED http_error status=%d url=%s\n"
                         % (exc.code, _base()))
        return 5
    except Exception as exc:                       # noqa: BLE001
        sys.stderr.write("[aidumem-stop] selftest FAILED unreachable url=%s err=%s\n"
                         % (_base(), exc))
        return 4

    # 写完必须回读：只看 /add 返回 200 不够，历史上出过「请求收下了、落库
    # 没发生」的形态，而那正是本钩子要防的那类静默。
    for _ in range(8):      # 异步落库，回读要给足耐心
        try:
            res = _call("/search", {"query": marker, "user_id": uid,
                                    "limit": 5, "metadata": {}})
        except Exception:                          # noqa: BLE001
            break
        for r in res.get("results") or []:
            if marker in (r.get("memory") or r.get("text") or ""):
                print("[aidumem-stop] selftest OK url=%s user_id=%s （写入并回读成功）"
                      % (_base(), uid))
                return 0
        time.sleep(1.5)

    sys.stderr.write(
        "[aidumem-stop] selftest FAILED write_not_readable url=%s user_id=%s\n"
        "  → /add 接受了请求，但随后搜不到刚写的内容。\n" % (_base(), uid))
    return 6


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        return _selftest()

    try:
        payload = json.load(sys.stdin)
    except Exception:                              # noqa: BLE001
        return 0
    if not isinstance(payload, dict):
        return 0
    # 宿主为防递归会在钩子自身触发的停止上置 stop_hook_active；此时别再写一遍。
    if payload.get("stop_hook_active"):
        return 0

    transcript = payload.get("transcript_path") or ""
    user_text, asst_text = _last_turn(transcript) if transcript else ("", "")
    if len(user_text.strip()) < MIN_CHARS:
        return 0

    turn = 0
    try:
        turn = int(payload.get("turn") or payload.get("turn_id") or 0)
    except (TypeError, ValueError):
        turn = 0

    _write(user_text, asst_text, str(payload.get("session_id") or ""), turn)
    return 0                                       # 永远 0：写失败不许打断宿主


if __name__ == "__main__":
    sys.exit(main())
