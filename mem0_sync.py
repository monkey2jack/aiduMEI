#!/usr/bin/env python3
"""
mem0_sync — Hermes MEMORY.md ↔ aiduMEM 双向同步引擎
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
J-space Aletheia 思想引擎的最后一环：
MEMORY.md 的每一次写入，实时同步到 aiduMEM 向量数据库。

— 设计 ─
1. inotify 监听 MEMORY.md 变化（200ms debounce）
2. § 分割条目 → hash 去重 → POST aiduMEM /add
3. aiduMEM 返回的 memory_id 记录在 .sync_state.json
4. 初始全量同步 + 后续增量同步
5. aiduMEM 挂了也不影响 Hermes — MEMORY.md 是 ground truth

— 运行 ─
  systemd: mem0-sync.service
  手动:   python3 mem0_sync.py --once     (全量同步一次)
  守护:   python3 mem0_sync.py --daemon   (持续监听)
"""

import hashlib, json, os, sys, time, logging, argparse
from pathlib import Path
from typing import Optional

# systemd / cron 的 cwd 未必是仓库根，先把仓库根补进 sys.path 再 import ducky
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 凭据与身份都走单一真相源（v19.4.2）：
#   · api_auth_headers()  环境变量 → .env 兜底，与服务端读同一份文件
#   · DEFAULT_USER_ID     记忆分区身份，源码里不写任何真实标识
# v19.4.1 的事故就是本文件自成一套：裸调 API 无凭证 → 门禁开启后每次写入
# 401，而失败只写日志、没人看，同步整整停摆 8 天。
from ducky.utils import DEFAULT_USER_ID, api_auth_headers

# ── 配置（全部可用环境变量覆盖）──
MEMORY_MD = Path(os.path.expanduser(
    os.environ.get("AIDUMEM_HOST_MEMORY_MD", "~/.hermes/memories/MEMORY.md")))
AIDUMEM_URL = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/") + "/add"
SYNC_STATE = Path(os.path.expanduser(
    os.environ.get("AIDUMEM_SYNC_STATE", "~/.hermes/memories/.sync_state.json")))
DEBOUNCE_S = 0.2
USER_ID = DEFAULT_USER_ID
LOG_FORMAT = "%(asctime)s [mem0_sync] %(message)s"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt="%H:%M:%S")
logger = logging.getLogger("mem0_sync")


# ═══════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════

def hash_entry(text: str) -> str:
    """对条目内容做短 hash"""
    return hashlib.md5(text.strip().encode()).hexdigest()[:12]


def parse_entries(content: str) -> list[tuple[str, str]]:
    """解析 MEMORY.md，§ 分割，返回 [(hash, text), ...]"""
    entries = []
    for part in content.split("§"):
        text = part.strip()
        if text and len(text) > 5:
            entries.append((hash_entry(text), text))
    return entries


def load_state() -> dict:
    """加载已同步状态 {hash: memory_id}"""
    if SYNC_STATE.exists():
        try:
            with open(SYNC_STATE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    """原子写入同步状态"""
    tmp = str(SYNC_STATE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, SYNC_STATE)


# ═══════════════════════════════════════════════
# REST 调用
# ═══════════════════════════════════════════════

def push_to_aidumem(text: str, category: str, source: str) -> bool:
    """POST aiduMEM /add（默认 async_mode=true，聊天体感起飞）

    2026-07-21：Hermes MEMORY.md → aiduMEM 写入默认异步接单。
    accepted / async_queued 也算成功（后台继续 LLM 抽取落库）。
    """
    import urllib.request, urllib.error
    payload = json.dumps({
        "messages": json.dumps([{"role": "user", "content": text}], ensure_ascii=False),
        "user_id": USER_ID,
        "async_mode": True,  # 聊天侧默认异步
        "metadata": {
            "category": category,
            "source": source,
            "async_mode": True,
            "caller": "mem0_sync",
        },
    }, ensure_ascii=False).encode("utf-8")
    try:
        # 异步接单通常 <1s；仍给 15s 兜底，避免偶发慢启动误判失败
        headers = {"Content-Type": "application/json"}
        headers.update(api_auth_headers())
        req = urllib.request.Request(
            AIDUMEM_URL,
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            status = data.get("status", "")
            action = data.get("action", "")
            job_id = data.get("job_id") or ""
            if status in ("ok", "accepted") or action in (
                "async_queued", "new", "updated", "fastpath", "merged", "direct"
            ):
                if job_id:
                    logger.debug(f"  aiduMEM async: job={job_id[:12]} | {text[:40]}...")
                else:
                    logger.debug(f"  aiduMEM: {action or status} | {text[:40]}...")
                return True
            logger.warning(f"  ❌ 未识别返回: {str(data)[:200]}")
            return False
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        if e.code in (401, 403):
            # 鉴权失败必须响亮报错：这是「同步悄悄不干活」的头号成因，
            # 降级成 warning 会淹没在日志里（v19.4.1 就是这么丢了 8 天增量）。
            logger.error(
                f"  ❌ HTTP {e.code} 鉴权失败：同步已停摆。"
                f"请确认 AIDUMEM_API_TOKEN 已注入本进程"
                f"（systemd 用 EnvironmentFile= 指向部署的 .env）| {body}"
            )
        else:
            logger.warning(f"  ❌ HTTP {e.code}: {body}")
        return False
    except Exception as e:
        logger.warning(f"  ❌ 网络异常: {e}")
        return False


# ═══════════════════════════════════════════════
# 同步逻辑
# ═══════════════════════════════════════════════

def sync_once() -> dict:
    """单次全量/增量同步。返回 {skipped, new, errors}"""
    if not MEMORY_MD.exists():
        logger.warning(f"MEMORY.md 不存在: {MEMORY_MD}")
        return {"skipped": 0, "new": 0, "errors": 0}

    content = MEMORY_MD.read_text()
    entries = parse_entries(content)
    state = load_state()

    skipped, new, errors = 0, 0, 0
    for h, text in entries:
        if h in state:
            skipped += 1
            continue
        success = push_to_aidumem(text, category="hermes_memory", source="mem0_sync")
        if success:
            state[h] = ""  # 标记已同步
            new += 1
            logger.info(f"  ✅ [{h}] | {text[:50]}...")
        else:
            errors += 1

    save_state(state)
    logger.info(f"同步完成: {skipped} 跳过 | {new} 新增 | {errors} 失败")
    return {"skipped": skipped, "new": new, "errors": errors}


# ═══════════════════════════════════════════════
# inotify 守护
# ═══════════════════════════════════════════════

def daemon_loop():
    """inotify 监听 MEMORY.md → debounce → sync"""
    try:
        from inotify_simple import INotify, flags
        inotify = INotify()
        watch_dir = str(MEMORY_MD.parent)
        wd = inotify.add_watch(watch_dir, flags.MODIFY | flags.CLOSE_WRITE | flags.CREATE)
        logger.info(f"👀 监听 {MEMORY_MD}")
    except ImportError:
        logger.error("需要 inotify_simple: pip install inotify_simple")
        sys.exit(1)

    last_sync = 0.0
    logger.info("=== mem0_sync daemon 启动 ===")
    while True:
        try:
            for event in inotify.read(timeout=1000):
                fname = event.name
                if fname and fname == MEMORY_MD.name:
                    logger.debug(f"检测到变化: {event}")
                    last_sync = time.time()
        except Exception as e:
            logger.warning(f"inotify read 异常: {e}")
            time.sleep(1)
            continue

        # debounce: 200ms 无新事件 → 同步
        if last_sync > 0 and time.time() - last_sync >= DEBOUNCE_S:
            last_sync = 0.0
            sync_once()


# ═══════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hermes MEMORY.md → aiduMEM 同步引擎")
    parser.add_argument("--once", action="store_true", help="全量同步一次后退出")
    parser.add_argument("--daemon", action="store_true", help="守护模式，inotify 实时监听")
    parser.add_argument("--reset", action="store_true", help="清除同步状态，全量重新同步")
    args = parser.parse_args()

    if args.reset and SYNC_STATE.exists():
        SYNC_STATE.unlink()
        logger.info("已清除同步状态")

    if args.once or args.reset:
        result = sync_once()
        logger.info(f"一次性同步: {result}")
        sys.exit(0 if result["errors"] == 0 else 1)

    if args.daemon:
        daemon_loop()
    else:
        # 默认 = --once
        sync_once()
