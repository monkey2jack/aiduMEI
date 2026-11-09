#!/usr/bin/env python3
"""Claude Code 自带记忆 → aiduMEI 桥（f0.8 ①）。

Claude Code（CC）有自己的 auto-memory：把普通 markdown 写进
``~/.claude/projects/<项目>/memory/``（``MEMORY.md`` 索引 + 若干主题文件），
每次开会话读 ``MEMORY.md``。这条路绕过了 aiduMEI 的写入闸门，密钥会明文落盘。
决定：不关它，接上它——

1. 打码：文件在本项目 CC 记忆目录里时，用 aiduMEI 原则 7 打码器
   （``ducky.write_policy.redact_forbidden``）扫一遍，有命中就原地改写
   （同目录临时文件 + 原子替换，保留权限位与属主）。
2. 入库：主题文件正文经正常鉴权的 ``/add`` 写进当前身份（服务端闸门照常生效），
   metadata 带 ``source=cc_auto_memory``、文件名、内容哈希。
   - 同一文件同一内容（哈希相同）不重复写；服务端已有同哈希/同正文的记忆也不写。
   - 文件改了：新内容写入后，删掉这个文件早先导入、哈希不同的那几条（新版取代旧版）。
   - ``MEMORY.md`` 只是索引：只打码，不入库；已裁成存根的文件（「已存入 aiduMEI：」）不入库。

入口：
    钩子   python3 integrations/claude-code/cc_memory_bridge.py hook  （stdin = CC PostToolUse 载荷）
    批量   scripts/cc_memory_sweep.py 复用本模块

本模块是独立集成件、只用标准库：钩子跑在宿主的 python3 里，不要求装了服务依赖。
打码器从仓库（``AIDUMEI_INSTALL_DIR``，缺省本文件上两级）按单文件加载
（不执行 ``ducky/__init__``，那会拉起整个引擎）；加载不到时不入库，只记一行日志。
凭据兜底链与读线/写线一致：环境变量 → AIDUMEM_ENV_FILE → AIDUMEM_HOME/.env →
~/.aidumem/.env → 仓库 .env；``<钥匙目录>/<身份>.key`` 在就用它。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(os.environ.get("AIDUMEI_INSTALL_DIR") or Path(__file__).resolve().parents[2])
DEFAULT_URL = "http://127.0.0.1:8767"
SOURCE = "cc_auto_memory"
AGENT = "aidumem-cc-memory-bridge"
INDEX_NAME = "MEMORY.md"
STUB_PREFIX = "已存入 aiduMEI："
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "")
    try:
        return max(minimum, int(raw)) if raw.strip() else default
    except ValueError:  # 非法值回退默认（钩子绝不因配置写错而崩）
        return default


def max_chars() -> int:
    """单个文件入库的字符上限（服务端原文上限默认 20000，留出余量）。"""
    return _int_env("AIDUMEI_CC_BRIDGE_MAX_CHARS", 12000, 500)


def http_timeout() -> int:
    """每个 HTTP 请求的超时秒数。f0.8.0 上线实测：生产 /search 要走一次远端嵌入 + 混合召回，
    4–6 秒是常态，原先写死 5 秒会让导入整体超时失败（只打码、不入库）。缺省 12 秒，
    钩子总超时（connect_host 写 30 秒）容得下 查重 + 写入 两次请求。"""
    return _int_env("AIDUMEI_CC_BRIDGE_TIMEOUT", 12, 1)


# ── 打码器：单文件加载 ducky.write_policy ─────────────────────────────────
_REDACT = None


def _redactor():
    global _REDACT
    if _REDACT is None:
        if "ducky" not in sys.modules:
            pkg = types.ModuleType("ducky")
            pkg.__path__ = [str(REPO / "ducky")]  # 命名空间壳：只加载用到的子模块
            sys.modules["ducky"] = pkg
        from ducky.write_policy import redact_forbidden
        _REDACT = redact_forbidden
    return _REDACT


def redact_text(text: str) -> tuple[str, list[str]]:
    return _redactor()(text)


# ── 路径判定 ───────────────────────────────────────────────────────────
def claude_home() -> Path:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(cfg) if cfg else Path(os.environ.get("HOME") or Path.home()) / ".claude"


def project_slug(cwd: str) -> str:
    """CC 的项目目录名：绝对路径里非字母数字一律换成 ``-``。"""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _expand_user_path(v: str) -> Path | None:
    v = (v or "").strip()
    if v.startswith("~/"):
        return Path(os.environ.get("HOME") or Path.home()) / v[2:]
    return Path(v) if v.startswith("/") else None


def auto_memory_setting(cwd: str = "") -> Path | None:
    """``autoMemoryDirectory``（CC 设置项，绝对路径或 ``~/`` 开头）：项目 local → 用户。

    f0.8.0 自审：**不读项目共享的 ``.claude/settings.json``**。那份文件随仓库提交，克隆来的仓库
    能借它把「记忆目录」指到 ``~/`` 或仓库本身——桥就会把 CC 在那里改的任意 .md 原地改写并导入
    记忆、注入以后所有会话。CC 自己也不从项目共享设置接受这一项（防共享仓库重定向记忆写入）。
    """
    files = []
    if cwd.startswith("/"):
        files.append(Path(cwd) / ".claude" / "settings.local.json")
    files.append(claude_home() / "settings.json")
    for f in files:
        try:
            v = json.loads(f.read_text(encoding="utf-8")).get("autoMemoryDirectory")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(v, str) and _expand_user_path(v) is not None:
            return _expand_user_path(v)
    return None


def git_root(cwd: str) -> Path | None:
    """cwd 所在 git 仓库的主工作树根（worktree 归到主仓：CC 记忆按仓库共用一份）。"""
    if not cwd.startswith("/"):
        return None
    p = Path(cwd)
    for d in (p, *p.parents):
        g = d / ".git"
        try:
            if g.is_dir():
                return d
            if g.is_file():
                txt = g.read_text(encoding="utf-8", errors="replace").strip()
                if not txt.startswith("gitdir:"):
                    return d
                gd = Path(txt.split(":", 1)[1].strip())
                gd = gd if gd.is_absolute() else (d / gd)
                common = gd / "commondir"
                if common.is_file():
                    c = Path(common.read_text(encoding="utf-8").strip())
                    main_git = (c if c.is_absolute() else gd / c).resolve()
                    return main_git.parent
                return d
        except OSError:
            return None
    return None


def memory_dirs_for(payload: dict) -> list[Path]:
    """本次会话所属项目可能的 CC 记忆目录（按优先级，去重）。

    ``AIDUMEI_CC_MEMORY_DIR`` 设了就只认它；否则依次：``autoMemoryDirectory`` 设置、
    ``CLAUDE_CODE_PROJECT_DIR_NAME``、按 git 仓库根推的项目名（CC 的口径：同一仓库的
    子目录与 worktree 共用一份）、transcript 所在项目目录、按 cwd 推的项目名。
    """
    env = os.environ.get("AIDUMEI_CC_MEMORY_DIR")
    if env:
        return [Path(env)]
    projects = claude_home() / "projects"
    cwd = str(payload.get("cwd") or "")
    out: list[Path] = []
    custom = auto_memory_setting(cwd)
    if custom is not None:
        out.append(custom)
    name = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME", "")
    if name and "/" not in name and name not in (".", ".."):
        out.append(projects / name / "memory")
    root = git_root(cwd)
    if root is not None:
        out.append(projects / project_slug(str(root)) / "memory")
    tp = str(payload.get("transcript_path") or "")
    if tp:
        parent = Path(tp).parent
        if parent.parent == projects and parent.name not in ("", ".", ".."):
            out.append(parent / "memory")
    if cwd.startswith("/"):
        out.append(projects / project_slug(cwd) / "memory")
    seen, uniq = set(), []
    for d in out:
        if str(d) not in seen:
            seen.add(str(d))
            uniq.append(d)
    return uniq


def memory_dir_for(payload: dict) -> Path | None:
    dirs = memory_dirs_for(payload)
    return dirs[0] if dirs else None


def resolve_inside(path: str | os.PathLike, root: Path) -> Path | None:
    """``path`` 是 ``root`` 里的普通 .md 文件时返回其真实路径，否则 None。

    拒绝：相对路径、``..`` 段、文件本身是软链、任一层解析后跳出 root、
    root 本身是软链、非普通文件、非 .md 后缀。
    """
    p = Path(path)
    if not p.is_absolute() or ".." in p.parts or p.suffix.lower() != ".md":
        return None
    try:
        if root.is_symlink() or not root.is_dir():
            return None
        real_root = root.resolve(strict=True)
        st = os.lstat(p)
        if not stat.S_ISREG(st.st_mode):  # 软链 / 目录 / 设备
            return None
        real = p.resolve(strict=True)
    except OSError:
        return None
    if os.path.commonpath([str(real_root), str(real)]) != str(real_root) or real == real_root:
        return None
    # 原路径按字面也必须在 root 下（防「root 外的软链目录恰好指回 root」这类歧义）
    try:
        p.relative_to(root)
    except ValueError:
        try:
            p.relative_to(real_root)
        except ValueError:
            return None
    return real


def hook_target(payload: dict) -> tuple[Path, Path] | None:
    """PostToolUse 载荷 → (文件真实路径, 记忆目录真实路径)；不是 CC 记忆写入就 None。"""
    if not isinstance(payload, dict) or payload.get("tool_name") not in _WRITE_TOOLS:
        return None
    ti = payload.get("tool_input") or {}
    fp = ti.get("file_path") if isinstance(ti, dict) else None
    if not fp:
        return None
    for root in memory_dirs_for(payload):
        real = resolve_inside(fp, root)
        if real is not None:
            return real, root.resolve()
    return None


# ── 原地打码 ───────────────────────────────────────────────────────────
def _read_nofollow(path: Path) -> str:
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def atomic_rewrite(path: Path, text: str) -> None:
    """同目录临时文件写好 → 复制权限位/属主 → os.replace。"""
    st = os.lstat(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except (OSError, AttributeError):
            pass  # 非 root 改不了属主时保持当前用户（与原文件同属主的常见情形无影响）
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def redact_file(path: Path, apply: bool = True) -> tuple[str, list[str]]:
    """返回 (打码后的正文, 命中类型)。有命中且 apply 时原地改写。"""
    text = _read_nofollow(path)
    new, kinds = redact_text(text)
    if kinds and new != text and apply:
        atomic_rewrite(path, new)
    return new, kinds


# ── 文件内容解析 ─────────────────────────────────────────────────────────
_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


def split_frontmatter(text: str) -> tuple[dict, str, str]:
    """返回 (frontmatter 键值, frontmatter 原文块, 正文)。只认简单 ``key: value``。

    嵌套一层的 ``parent:`` 下缩进的 ``key: value`` 记成 ``"parent.key"``（Claude Code 2.1.319
    起把 ``type`` 挪进了 ``metadata:`` 块）。顶层键不受影响，内容哈希只看顶层键，旧文件不会重导。
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, "", text
    fm = {}
    parent = ""
    for ln in m.group(1).splitlines():
        if ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        k, v = k.strip(), v.strip().strip("'\"")
        if not ln.startswith((" ", "\t")):
            fm[k] = v
            parent = k if not v else ""
        elif parent and k and not k.startswith("-"):
            fm.setdefault(f"{parent}.{k}", v)
    return fm, m.group(0), text[m.end():]


def fm_type(fm: dict) -> str:
    """记忆类型：顶层 ``type`` 优先，其次 ``metadata.type``（CC 2.1.319 格式）。"""
    return fm.get("type") or fm.get("metadata.type") or ""


def file_title(path: Path, text: str) -> str:
    fm, _, body = split_frontmatter(text)
    if fm.get("name"):
        return fm["name"][:120]
    for ln in body.splitlines():
        if ln.startswith("#"):
            t = ln.lstrip("#").strip()
            if t:
                return t[:120]
    return path.stem[:120]


def is_stub(text: str) -> bool:
    _, _, body = split_frontmatter(text)
    return body.strip().startswith(STUB_PREFIX) and len(body.strip().splitlines()) <= 2


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# CC 每次写记忆文件都会在 frontmatter 里盖 ``modified`` 时间戳：哈希只算稳定部分，
# 否则「内容没变、只是又存了一次」也会被当成新版本反复导入。
_STABLE_FM_KEYS = ("name", "description", "type")


def content_sha(text: str) -> str:
    fm, _, body = split_frontmatter(text)
    stable = "\n".join(f"{k}:{fm.get(k, '')}" for k in _STABLE_FM_KEYS) + "\n" + normalize(body)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


# ── 身份与凭据（与 aidumem-cc-*.sh 三线同源）───────────────────────────────
def _env_candidates() -> list[Path]:
    out = []
    for c in (os.environ.get("AIDUMEM_ENV_FILE"),
              (os.environ.get("AIDUMEM_HOME") or "") and os.path.join(os.environ["AIDUMEM_HOME"], ".env"),
              os.environ.get("HOME") and os.path.join(os.environ["HOME"], ".aidumem/.env"),
              str(REPO / ".env"), ".env"):
        if c:
            out.append(Path(c))
    return out


def env_lookup(key: str) -> str:
    if os.environ.get(key):
        return os.environ[key]
    for cand in _env_candidates():
        try:
            lines = cand.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln in lines:
            ln = ln.strip()
            if ln.startswith("export "):
                ln = ln[7:].lstrip()
            if ln.startswith(key) and ln[len(key):].lstrip().startswith("="):
                v = ln.split("=", 1)[1].strip().strip("'\"")
                if v:
                    return v
    return ""


def identity() -> dict:
    url = (os.environ.get("AIDUMEM_URL") or os.environ.get("AIDUMEI_URL") or DEFAULT_URL).rstrip("/")
    uid = (os.environ.get("AIDUMEM_USER_ID") or os.environ.get("AIDUMEM_DEFAULT_USER_ID")
           or env_lookup("AIDUMEM_USER_ID") or env_lookup("AIDUMEM_DEFAULT_USER_ID") or "default")
    bid = os.environ.get("AIDUMEI_BANK_ID") or env_lookup("AIDUMEI_BANK_ID") or "default"
    token = env_lookup("AIDUMEM_API_TOKEN")
    # f0.7.3：一个 agent 一把钥匙；身份名只认安全字符，防「../」读到别的文件
    kdir = os.environ.get("AIDUMEI_KEYS_DIR") or (
        os.path.join(os.environ["HOME"], ".aidumei-keys") if os.environ.get("HOME") else "")
    if kdir and _SAFE_ID.match(uid) and not uid.startswith("."):
        kf = Path(kdir) / f"{uid}.key"
        try:
            k = kf.read_text(encoding="utf-8").splitlines()[0].strip()
            if k:
                token = k
        except (OSError, IndexError):
            pass
    return {"url": url, "user_id": uid, "bank_id": bid, "token": token}


class Client:
    """最小 HTTP 客户端：每个请求都带 caller_user_id（Bearer 写入必须声明调用方）。"""

    def __init__(self, ident: dict, timeout: float | None = None):
        self.ident = ident
        self.timeout = float(http_timeout() if timeout is None else timeout)

    def _scope(self) -> dict:
        uid = self.ident["user_id"]
        return {"user_id": uid, "caller_user_id": uid, "bank_id": self.ident["bank_id"]}

    def post(self, path: str, body: dict, extra_headers: dict | None = None) -> tuple[int, dict]:
        h = {"Content-Type": "application/json", **(extra_headers or {})}
        if self.ident.get("token"):
            h["Authorization"] = "Bearer " + self.ident["token"]
        req = urllib.request.Request(self.ident["url"] + path,
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError:
                return exc.code, {}

    def add(self, content: str, metadata: dict, idem_key: str) -> tuple[int, dict]:
        body = {"messages": [{"role": "user", "content": content}], **self._scope(),
                "metadata": metadata, "idempotency_key": idem_key}
        return self.post("/add", body)

    def search(self, query: str, limit: int = 20) -> list[dict]:
        code, d = self.post("/search", {"query": query[:500], **self._scope(), "limit": limit})
        return list(d.get("results") or []) if code == 200 else []

    def delete(self, memory_id: str) -> bool:
        code, _ = self.post("/delete", {"memory_id": memory_id, **self._scope()})
        return code == 200


def _meta(item: dict) -> dict:
    md = item.get("metadata")
    return md if isinstance(md, dict) else {}


def _mem_text(item: dict) -> str:
    return str(item.get("memory") or item.get("text") or item.get("content") or "")


# ── 状态文件（每个身份 + 文件一条：最后导入的内容哈希）────────────────────────
def state_path() -> Path:
    p = os.environ.get("AIDUMEI_CC_BRIDGE_STATE")
    if p:
        return Path(p)
    return Path(os.environ.get("HOME") or Path.home()) / ".aidumem" / "cc-bridge-state.json"


def load_state(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or state_path()).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: Path | None = None) -> None:
    p = path or state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def state_key(ident: dict, root: Path, rel: str) -> str:
    return "|".join((ident["user_id"], ident["bank_id"], root.parent.name, rel))


# ── 入库 ──────────────────────────────────────────────────────────────
def build_payload(path: Path, root: Path, text: str) -> dict:
    """单个主题文件 → /add 的 content / metadata / 幂等键（纯函数，便于测试）。"""
    rel = path.relative_to(root).as_posix()
    title = file_title(path, text)
    fm, _, body = split_frontmatter(text)
    desc = fm.get("description", "")
    body = body.strip()
    limit = max_chars()
    truncated = len(body) > limit
    if truncated:
        body = body[:limit].rstrip() + "\n…（已截断）"
    head = f"[Claude Code 记忆 · {title}]" + (f"\n{desc}" if desc else "")
    sha = content_sha(text)
    md = {"source": SOURCE, "cc_file": rel[:200], "cc_title": title, "cc_sha": sha[:32],
          "cc_project": root.parent.name[:200], "_origin_agent": AGENT}
    if fm_type(fm):
        md["cc_type"] = fm_type(fm)[:40]
    if truncated:
        md["cc_truncated"] = 1
    return {"content": f"{head}\n\n{body}", "metadata": md, "sha": sha, "rel": rel, "title": title}


def _same_file(item: dict, pl: dict) -> bool:
    md = _meta(item)
    return md.get("source") == SOURCE and md.get("cc_file") == pl["metadata"]["cc_file"] \
        and md.get("cc_project") == pl["metadata"]["cc_project"]


def find_duplicate(client: Client, pl: dict) -> dict | None:
    """服务端已有同哈希（同一文件同一内容）或同正文的记忆 → 返回它。"""
    want = normalize(pl["content"])
    for it in client.search(pl["title"] + " " + pl["content"][:200], limit=10):
        if _meta(it).get("cc_sha") == pl["metadata"]["cc_sha"] or normalize(_mem_text(it)) == want:
            return it
    return None


def supersede(client: Client, pl: dict, old_title: str = "") -> int:
    """删掉同一文件早先导入、哈希不同的记忆；返回删除条数。"""
    n, seen = 0, set()
    for q in {pl["title"], old_title or pl["title"], pl["metadata"]["cc_file"]}:
        for it in client.search(q, limit=20):
            mid = str(it.get("id") or "")
            if not mid or mid in seen or not _same_file(it, pl):
                continue
            seen.add(mid)
            if _meta(it).get("cc_sha") != pl["metadata"]["cc_sha"] and client.delete(mid):
                n += 1
    return n


def ingest_file(path: Path, root: Path, text: str, client: Client, state: dict, *,
                session_id: str = "", apply: bool = True) -> dict:
    """单文件入库决策。返回 {"action": ..., ...}；apply=False 只说会做什么。"""
    rel = path.relative_to(root).as_posix()
    if path.name == INDEX_NAME and path.parent == root:
        return {"action": "skip", "reason": "index", "file": rel}
    if not text.strip():
        return {"action": "skip", "reason": "empty", "file": rel}
    if is_stub(text):
        return {"action": "skip", "reason": "stub", "file": rel}
    pl = build_payload(path, root, text)
    key = state_key(client.ident, root, rel)
    prev = state.get(key) or {}
    if prev.get("sha") == pl["sha"]:
        return {"action": "skip", "reason": "unchanged", "file": rel}
    if not apply:
        return {"action": "would_ingest", "file": rel, "supersedes": bool(prev.get("sha"))}
    dup = find_duplicate(client, pl)
    if dup is not None:
        state[key] = {"sha": pl["sha"], "title": pl["title"], "at": int(time.time()), "status": "duplicate"}
        return {"action": "skip", "reason": "duplicate", "file": rel}
    md = dict(pl["metadata"])
    if session_id:
        md["_origin_session_id"] = session_id[:128]
    idem = "ccmem-" + hashlib.sha256(f"{key}|{pl['sha']}".encode()).hexdigest()[:40]
    code, resp = client.add(pl["content"], md, idem)
    if code == 422:  # 服务端闸门整条拒收：记下哈希，同一内容不再重试
        state[key] = {"sha": pl["sha"], "title": pl["title"], "at": int(time.time()), "status": "rejected"}
        return {"action": "rejected", "file": rel, "detail": str(resp.get("detail") or "")[:200]}
    if code != 200:
        return {"action": "error", "file": rel, "code": code}
    removed = supersede(client, pl, prev.get("title", "")) if prev.get("sha") else 0
    state[key] = {"sha": pl["sha"], "title": pl["title"], "at": int(time.time()), "status": "imported"}
    return {"action": "ingested", "file": rel, "superseded": removed,
            "status": str(resp.get("status") or resp.get("action") or "")}


# ── 钩子入口 ───────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    d = os.environ.get("AIDUMEM_LOG_DIR") or (
        os.path.join(os.environ["HOME"], ".aidumem", "logs") if os.environ.get("HOME") else "/tmp")
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "aidumem-cc-memory-bridge.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def run_hook(payload: dict, client: Client | None = None) -> dict:
    tgt = hook_target(payload)
    if tgt is None:
        return {"action": "ignored"}
    path, root = tgt
    try:
        text, kinds = redact_file(path, apply=True)
    except ImportError:  # 拷出仓库独立运行、找不到打码器：不打码就不入库（服务端闸门另有一道）
        return {"action": "skip", "reason": "redactor_unavailable", "file": path.name, "redacted": []}
    out = {"redacted": kinds}
    if os.environ.get("AIDUMEI_CC_BRIDGE_INGEST", "1").strip().lower() in ("0", "off", "false", "no"):
        out.update(action="redact_only", file=path.name)
        return out
    client = client or Client(identity())
    sid = str(payload.get("session_id") or "")
    prefix = os.environ.get("AIDUMEI_CC_SESSION_PREFIX", "cc-")
    sid = (sid if sid.startswith(prefix) else prefix + sid) if sid else ""
    state = load_state()
    try:
        res = ingest_file(path, root, text, client, state, session_id=sid)
    except (OSError, ValueError) as exc:
        res = {"action": "error", "file": path.name, "detail": type(exc).__name__}
    if res.get("action") in ("ingested", "rejected") or res.get("reason") == "duplicate":
        save_state(state)
    out.update(res)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "hook":
        print("用法: cc_memory_bridge.py hook  （stdin = Claude Code PostToolUse 载荷）", file=sys.stderr)
        return 2
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    try:
        res = run_hook(payload)
    except Exception as exc:  # 绝不拖累宿主：任何异常只记一行
        _log(f"error {type(exc).__name__}")
        return 0
    if res.get("action") != "ignored":
        _log(json.dumps({k: res.get(k) for k in ("action", "reason", "file", "redacted", "superseded", "code", "detail")},
                        ensure_ascii=False))
    if res.get("redacted"):
        msg = (f"aiduMEI：记忆文件 {res.get('file') or ''} 里有 {len(res['redacted'])} 处密钥/证件片段，"
               "已原地打码。不要把凭据写进记忆文件。")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}},
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
