#!/usr/bin/env python3
"""
aiduMEM × Claude Code Hook
===========================
在 Claude Code（claude.ai/code）工作流中，在 context 压缩前自动把重要代码存入
aiduMEM Raw Drawer，防止 AI 遗忘已看过的代码。

集成方式：
  Claude Code 支持在 .claude/settings.json 中注册 hook 脚本。
  本脚本可作为 pre_compact hook 使用。

使用方式（手动）：
  python3 claude-code-hook.py store --file ./my_module.py
  python3 claude-code-hook.py store --content "def foo(): ..." --desc "helper function"
  python3 claude-code-hook.py search "authentication logic"
  python3 claude-code-hook.py impact ./api_server.py

环境变量：
  AIDUMEM_URL        API 地址，默认 http://127.0.0.1:8767
  AIDUMEM_API_TOKEN  API 鉴权 token（服务端开启门禁后必需）
  AIDUMEM_ENV_FILE   .env 路径，用于兜底读取 token 与身份
  AIDUMEM_USER_ID    用户命名空间，可由 .env 兜底，默认 default
  AIDUMEM_DEFAULT_USER_ID  AIDUMEM_USER_ID 缺省时的回落值（服务端同名键）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AIDUMEM_URL = os.environ.get("AIDUMEM_URL", "http://127.0.0.1:8767").rstrip("/")
TIMEOUT = int(os.environ.get("AIDUMEM_TIMEOUT", "10"))


# ── 凭据与身份（v19.4.2 补齐）─────────────────────────────
# 在仓库内运行时复用 ducky.utils（单一真相源，自带 .env 兜底）；
# 本文件设计为可拷贝到 .claude/ 等编辑器配置目录独立运行，
# 因此保留一份最小兜底实现，import 失败时不至于变回裸奔。
#
# 凭据和身份必须走**同一条链**。若只有 token 兜底、身份只认环境变量，
# 那么编辑器以空环境拉起本 hook 时就是「凭据对、租户错」：
# 写入成功、落进 default 分区，用户在自己分区里怎么查都查不到，
# 而且全程没有任何报错。
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from ducky.utils import DEFAULT_USER_ID, api_auth_headers  # type: ignore

    def _env_or_env_file(key: str, default: str = "") -> str:
        from ducky.utils import env_or_env_file  # type: ignore
        return env_or_env_file(key, default)
except Exception:                                     # pragma: no cover - 拷出仓库的场景
    def _read_env_file(path: str) -> dict:
        """最小 .env 解析：兼容 `export KEY=VALUE`、引号、BOM、# 注释。"""
        parsed: dict = {}
        try:
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip().lstrip("﻿")
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    if line.startswith("export "):
                        line = line[len("export "):].strip()
                    key, _, value = line.partition("=")
                    key = key.strip()
                    if key:
                        parsed.setdefault(key, value.strip().strip('"').strip("'"))
        except OSError:
            return {}
        return parsed

    def _env_or_env_file(key: str, default: str = "") -> str:   # type: ignore[misc]
        val = os.environ.get(key, "").strip()
        if val:
            return val
        home = os.environ.get("AIDUMEM_HOME", "")
        for cand in (
            os.environ.get("AIDUMEM_ENV_FILE", ""),
            os.path.join(home, ".env") if home else "",
            os.path.expanduser("~/.aidumem/.env"),
            ".env",
        ):
            if not cand or not os.path.isfile(cand):
                continue
            val = str(_read_env_file(cand).get(key, "")).strip()
            if val:
                return val
        return default

    DEFAULT_USER_ID = _env_or_env_file("AIDUMEM_DEFAULT_USER_ID", "default")  # type: ignore[misc]

    def api_auth_headers() -> dict:                   # type: ignore[misc]
        token = _env_or_env_file("AIDUMEM_API_TOKEN")
        return {"Authorization": f"Bearer {token}"} if token else {}


# 身份与全局单一真源对齐（见 ducky/utils.py DEFAULT_USER_ID）。
AIDUMEM_USER_ID = _env_or_env_file("AIDUMEM_USER_ID") or DEFAULT_USER_ID


def _auth_hint(result: dict) -> dict:
    """把 401/403 翻译成人话 —— 否则用户只看到 `HTTP 401` 三个字，无从下手。"""
    if str(result.get("error", "")).startswith(("HTTP 401", "HTTP 403")):
        result["hint"] = ("鉴权失败：请设置 AIDUMEM_API_TOKEN，"
                          "或让 AIDUMEM_ENV_FILE 指向部署的 .env")
    return result


def _post(path: str, body: dict) -> dict:
    url = f"{AIDUMEM_URL}{path}"
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    headers.update(api_auth_headers())
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return _auth_hint({"error": f"HTTP {e.code}",
                           "detail": e.read().decode(errors="replace")})
    except Exception as e:
        return {"error": str(e)}


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{AIDUMEM_URL}{path}"
    if params:
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{qs}"
    headers = {"Accept": "application/json"}
    headers.update(api_auth_headers())
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return _auth_hint({"error": f"HTTP {e.code}",
                           "detail": e.read().decode(errors="replace")})
    except Exception as e:
        return {"error": str(e)}


def cmd_store(args: argparse.Namespace) -> None:
    """存储代码到 Raw Drawer。"""
    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"❌ 文件不存在: {args.file}", file=sys.stderr)
            sys.exit(1)
        content = path.read_text(errors="replace")[:10000]  # 截断超大文件
        content = f"FILE: {args.file}\nLINES: {content.count(chr(10))}\n---\n{content}"
        if args.desc:
            content = f"DESCRIPTION: {args.desc}\n{content}"
    elif args.content:
        content = args.content
        if args.desc:
            content = f"DESCRIPTION: {args.desc}\n\n{content}"
    else:
        # 从 stdin 读取
        content = sys.stdin.read()

    result = _post("/add/raw", {
        "content": content,
        "source": "claude_code_hook",
        "user_id": AIDUMEM_USER_ID,
    })
    if "error" in result:
        print(f"❌ 存储失败: {result['error']}", file=sys.stderr)
        if result.get("hint"):
            print(f"   {result['hint']}", file=sys.stderr)
        sys.exit(1)
    print(f"✅ 存入 Raw Drawer: id={result.get('id', '?')}")


def cmd_search(args: argparse.Namespace) -> None:
    """搜索记忆。"""
    result = _post("/search", {
        "query": args.query,
        "user_id": AIDUMEM_USER_ID,
        "top_k": args.top_k,
    })
    if "error" in result:
        print(f"❌ 搜索失败: {result['error']}", file=sys.stderr)
        if result.get("hint"):
            print(f"   {result['hint']}", file=sys.stderr)
        sys.exit(1)
    memories = result.get("results", [])
    print(f"🔍 找到 {len(memories)} 条记忆：\n")
    for i, m in enumerate(memories, 1):
        score = m.get("score", 0)
        text = m.get("memory", "")[:200]
        print(f"[{i}] score={score:.3f}\n    {text}\n")


def cmd_impact(args: argparse.Namespace) -> None:
    """分析文件改动的波及范围。"""
    result = _post("/code/impact", {"file_path": args.file})
    if "error" in result:
        print(f"❌ 分析失败: {result['error']}", file=sys.stderr)
        if result.get("hint"):
            print(f"   {result['hint']}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_health(args: argparse.Namespace) -> None:
    """检查 aiduMEM 健康状态。"""
    result = _get("/health")
    status = result.get("health_status", "unknown")
    version = result.get("version", "?")
    print(f"{'✅' if status == 'ok' else '❌'} aiduMEM {version} — {status}")
    if result.get("degraded"):
        print(f"⚠️  降级模块: {result['degraded']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="aiduMEM × Claude Code Hook CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subs = parser.add_subparsers(dest="command", required=True)

    # store
    p_store = subs.add_parser("store", help="存储代码/文本到 Raw Drawer")
    p_store.add_argument("--file", "-f", help="代码文件路径")
    p_store.add_argument("--content", "-c", help="直接传入文本内容")
    p_store.add_argument("--desc", "-d", help="描述/摘要")
    p_store.set_defaults(func=cmd_store)

    # search
    p_search = subs.add_parser("search", help="语义搜索记忆")
    p_search.add_argument("query", help="搜索关键词")
    p_search.add_argument("--top-k", "-k", type=int, default=5, dest="top_k")
    p_search.set_defaults(func=cmd_search)

    # impact
    p_impact = subs.add_parser("impact", help="分析文件改动波及范围")
    p_impact.add_argument("file", help="要分析的文件路径")
    p_impact.set_defaults(func=cmd_impact)

    # health
    p_health = subs.add_parser("health", help="检查服务健康状态")
    p_health.set_defaults(func=cmd_health)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
