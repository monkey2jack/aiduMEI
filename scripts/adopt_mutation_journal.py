#!/usr/bin/env python3
"""mutation journal 遗留库受控收养工具（f0.4++ WP1 · cc 迁移场景工程化）。

**为什么需要这个工具**：

从旧宿主（如另一台生产机）整体迁移 data/ 到新宿主（如 cc 沙箱）后，
mutation_journal.sqlite3 是「遗留库」——journal 本体在，但配对的身份标记
（mutation_journal.identity.json + 库内 journal_identity 表）在迁移中未随迁
或从未建立。此时 api_server 启动会在 startup_recover() 处 fail-closed：

    JournalEvidenceError: journal_legacy_adoption_required

这是**设计如此的证据链保护**（DB 与 identity 是一对，双双缺失时无法与
「首次安装」区分，必须靠外部部署/备份清单人工裁决）。2026-10-16 本侧迁移
后曾以一次性 python 手工收养救火——能用，但不可复现、无证据留痕、且绕过
了「所有写入者已停止」的核验。本工具把该动作工程化：

  1. **活体 API 守卫**：服务必须已停（探活不通才继续）；
  2. **进程锁互斥**：尝试独占 .api-process.lock（非阻塞），拿不到=有活
     API 进程，立即拒绝——与 acquire_api_process_lock 同一把锁；
  3. **只读深度核验**：quick_check、schema v1、状态集合合法、确认库内确实
     没有 journal_identity 表（防止对已收养库重复收养）；
  4. **收养**：initialize_journal(adopt_existing=True) 建立 identity 配对；
  5. **证据输出**：stdout 打印收养回执（记录数、时间范围、identity、
     核验结论），--json 供机读归档。

**安全边界**：本工具永不修改 mutations 表内容、永不重放、永不删库。
它只做一件事：为经验证的遗留库补建身份标记。对健康库是 no-op，
对已收养库直接跳过，对损坏库 fail-closed 原样报错。

用法（必须先停服务）：
    cd <aiduMEI 安装目录>
    set -a; . ./.env; set +a
    venv/bin/python scripts/adopt_mutation_journal.py            # 人读回执
    venv/bin/python scripts/adopt_mutation_journal.py --json     # 机读
    venv/bin/python scripts/adopt_mutation_journal.py --url http://127.0.0.1:8767

退出码：0=收养成功或已收养（幂等）；1=守卫拒绝（服务活着/锁被占）；
2=核验失败（库损坏/状态非法）；3=收养过程异常。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 仓内运行：把仓库根挂进 sys.path（scripts/ 的上一级）
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 凭据单一真源：本服务门禁可能开启，探活也必须带凭据（否则 401 与「服务没起」
# 无法区分）。复用 ducky.utils.api_auth_headers —— 它自带环境变量 → .env 兜底，
# 不各自实现凭据读取（scripts/ 的守卫要求）。
from ducky.utils import api_auth_headers as _auth_headers  # noqa: E402


def _api_alive(url: str, timeout: float) -> bool:
    """探活：任何 HTTP 响应都算活着（含 401/503），连接拒绝才算停。

    带凭据探 /health：门禁开启时不带 token 会得到 401，虽仍属「活着」的证据，
    但带上凭据能顺带确认服务是真的在服务，而不是只开着端口。
    """
    req = urllib.request.Request(url.rstrip("/") + "/health",
                                 headers=_auth_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 500 or True  # 有响应=进程在
    except urllib.error.HTTPError:
        return True  # 4xx/5xx 也是活着的证据
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _no_live_api_process(data_dir: Path) -> tuple[bool, str]:
    """非阻塞抢 .api-process.lock：抢到=无活 API 进程（立即释放）。"""
    import fcntl

    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / ".api-process.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True, "api-process lock acquired (no live API process)"
    except OSError:
        return False, "api-process lock held by a live process"
    finally:
        os.close(fd)  # 释放锁；不 unlink（与 process_lock.py 同纪律）


def main() -> int:
    ap = argparse.ArgumentParser(description="Adopt a validated legacy mutation journal")
    ap.add_argument("--url", default=os.environ.get("AIDUMEI_URL", "http://127.0.0.1:8767"),
                    help="API base url for the liveness guard (default: %(default)s)")
    ap.add_argument("--timeout", type=float, default=3.0, help="probe timeout seconds")
    ap.add_argument("--yes", action="store_true",
                    help="确认已阅读回执语义（交互留痕；CI/守护脚本可带上）")
    ap.add_argument("--json", action="store_true", help="machine-readable receipt on stdout")
    args = ap.parse_args()

    receipt: dict = {"tool": "adopt_mutation_journal", "version": 1, "ts": time.time()}

    def _emit(code: int, status: str, **kw):
        receipt.update(status=status, exit_code=code, **kw)
        if args.json:
            print(json.dumps(receipt, ensure_ascii=False, default=str))
        else:
            for k, v in receipt.items():
                print(f"  {k}: {v}")
        return code

    # ── 守卫 1：活体 API ────────────────────────────────
    if _api_alive(args.url, args.timeout):
        return _emit(1, "refused_api_alive",
                     detail=f"{args.url} 仍在响应。先停服务再收养（写入者必须全部停止）。")

    # ── 守卫 2：进程锁互斥 ──────────────────────────────
    from ducky.utils import DATA_DIR
    from ducky.mutation_journal import journal_path, journal_identity_path

    ok, detail = _no_live_api_process(Path(DATA_DIR))
    if not ok:
        return _emit(1, "refused_lock_held", detail=detail)
    receipt["guard_lock"] = detail

    jpath = journal_path()
    ipath = journal_identity_path(jpath)
    receipt["journal_path"] = str(jpath)

    # ── 幂等：已收养直接跳过 ────────────────────────────
    if ipath.exists():
        return _emit(0, "already_adopted",
                     detail=f"identity 标记已存在（{ipath.name}），无需重复收养。")

    if not jpath.exists():
        return _emit(0, "fresh_install",
                     detail="journal 库不存在，属首次安装，服务启动时会自行建立证据，无需收养。")

    # ── 核验：只读深度检查遗留库 ────────────────────────
    try:
        from ducky.mutation_journal import _read_connection  # noqa: PLC2701 — 仓内工具复用
        conn = _read_connection(jpath, deep=True, legacy=True)
        try:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='journal_identity'"
            ).fetchone():
                return _emit(2, "refused_identity_table_present",
                             detail="库内已有 journal_identity 表但外部标记缺失——"
                                    "证据链半损，不属本工具的收养语义，人工核查备份清单。")
            n = conn.execute("SELECT COUNT(*) FROM mutations").fetchone()[0]
            span = conn.execute(
                "SELECT MIN(created_at), MAX(created_at) FROM mutations"
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # JournalEvidenceError / sqlite3.DatabaseError 等
        return _emit(2, "verification_failed", detail=f"{type(exc).__name__}: {exc}")

    receipt["mutations_count"] = n
    receipt["created_at_span"] = [span[0], span[1]]

    # ── 收养 ───────────────────────────────────────────
    try:
        from ducky.mutation_journal import initialize_journal, journal_health
        initialize_journal(adopt_existing=True)
    except Exception as exc:
        return _emit(3, "adoption_failed", detail=f"{type(exc).__name__}: {exc}")

    # ── 收养后复核：健康检查必须 ok ─────────────────────
    health = journal_health(deep=True)
    receipt["post_adoption_health"] = health.get("integrity", "unknown")
    if health.get("integrity") != "ok":
        return _emit(3, "post_adoption_unhealthy", detail=str(health))

    return _emit(0, "adopted",
                 detail=f"遗留 journal 已收养：{n} 条历史记录原样保留，identity 配对建立，"
                        f"深度完整性核验 ok。现在可以启动 api_server。")


if __name__ == "__main__":
    sys.exit(main())
