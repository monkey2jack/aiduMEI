#!/usr/bin/env python3
"""
scripts/release_scan.py — 发布前七面敏感内容扫描器
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

存在的理由
──────────
一个项目会把内部信息泄露到七个**互相独立**的公开面上。把工作区文件改干净，
只解决了第一面。其余六面里有一半是**发出去就改不回来的历史快照**：

    ① 工作区        —— 受控文件的内容（可改）
    ② 提交信息      —— 全部提交的 message（可重写，但重写后旧 SHA 仍可直连）
    ③ 标签注释      —— 全部 tag 的 annotation（可重写，同上）
    ④ 发布说明      —— 代码托管平台上的 Release 正文（可编辑）
    ⑤ 发行包内容    —— 上传到包索引的 sdist / wheel 解包后的文件（**不可覆盖**）
    ⑥ 包索引渲染面  —— PKG-INFO / METADATA 渲染成的项目网页正文
                       （**不下载就能看见**，最容易被忽略的一面）
    ⑦ 对外可见归档  —— 强制推送后仍可按 SHA 直达的孤儿提交、第三方镜像与缓存

**发行包这一面没有退路**：同一个版本号在包索引上永久不可覆盖、不可修改。
所以扫描的位置只有一个是对的 —— **上传之前**。

设计上的三个硬约束
──────────────────
1. **词表绝不入仓。**
   词表里是真实人名、部署地点这类东西 —— 它本身就是要藏的信息。
   把它写进这个文件，等于为了防泄露而制造一次泄露。
   所以词表一律外置（环境变量或仓库外的文件），本模块只有机制、没有内容。

2. **空词表必须拒绝运行，而不是放行。**
   一个没配词表的扫描器会报「0 命中」，和一个真的干净的项目**长得一模一样**。
   这种沉默的通过比不扫更危险，因为它发放「已经检查过了」的凭证。
   所以：词表为空 → 退出码 2，不输出任何结果。

3. **自检不过就不准出结果（负向对照焊进代码）。**
   同理，一个坏掉的扫描器也报「0 命中」。所以每次真扫之前，先在两个
   **内置的合成样本**上双向验证：脏样本必须报警，干净样本必须不报警。
   任何一边不符 → 退出码 3，**绝不输出那个 0**。
   合成样本用的是假词（见 CANARY），可以安全地留在仓库里。

豁免标记
────────
测试夹具里会出现故意造的假密钥（用来验证密钥检测规则真的会拒绝它）。
不给这类命中留出口，扫描器就会长期红着 —— 而一个长期红着的闸门，
等于训练所有人忽略它的输出，比没有闸门更糟。

出口只有一个，且刻意做得很窄：把 ALLOW_MARK 写在**命中所在的那一行**。

    v, _ = rule_screen("general", "k", "token=ghp_x")   # release-scan:allow 合成夹具

三条约束保证它不会变成后门：
  - **只认本行**。没有文件级、更没有目录级豁免 —— 目录级豁免会让新增文件
    自动继承赦免，是守卫射程小于缺陷分布的经典成因。
  - **豁免仍然出现在报告里**，单独成节、逐条列出。它从「失败计数」里消失，
    但绝不从「视野」里消失。
  - 加标记是一次**要过评审的显式改动**，diff 里看得见。

用法
────
    # 词表来源（二选一，都没有则拒绝运行）
    export AIDUMEI_SCAN_WORDS='词一|词二|词三'
    export AIDUMEI_SCAN_WORDLIST=/path/to/wordlist.txt   # 每行一词，# 开头为注释

    python3 scripts/release_scan.py <目录> [<目录> ...]
    python3 scripts/release_scan.py --selftest          # 只跑自检

退出码
──────
    0  自检通过，且全部目标零硬命中
    1  自检通过，但存在硬命中（**不允许发布**）
    2  词表缺失或为空（拒绝运行）
    3  自检失败 —— 扫描器本身不可信，结果作废
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

# ── 自检用的合成词。故意长、故意无意义，不可能在真实内容里自然出现。 ──
# 它们是**假词**，因此这个文件本身可以安全入仓 —— 这正是词表外置的意义。
CANARY = "AIDUMEI-SELFTEST-CANARY-7F3A9C21"
CANARY_CLEAN = "AIDUMEI-SELFTEST-BENIGN-0000"

# 七个公开面的规范名称。测试会断言这份清单不被悄悄缩短。
SURFACES = (
    "① 工作区",
    "② 提交信息",
    "③ 标签注释",
    "④ 发布说明",
    "⑤ 发行包内容",
    "⑥ 包索引渲染面",
    "⑦ 对外可见归档",
)

# ── 结构化模式。这些不含任何具体的敏感内容，可以入仓。 ──
# IPv4：八位组必须真的 ≤255。少了这个边界，SVG 的路径数据（形如
# "315.225.69.825"）会被当成 IP 报出来，用假红淹掉真红。
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4 = rf"(?<![\d.]){_OCTET}\.{_OCTET}\.{_OCTET}\.{_OCTET}(?![\d.])"

# 保留地址、私有网段与 RFC 5737 文档用地址不算泄露 —— 它们本来就是占位符。
_RESERVED = re.compile(
    r"^(?:0\.|10\.|127\.|169\.254\.|"
    r"172\.(?:1[6-9]|2\d|3[01])\.|192\.168\.|"
    r"192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|"
    r"22[4-9]\.|2[3-5]\d\.|"
    r"100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.)"
)

PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("公网IPv4", re.compile(_IPV4.encode())),
    ("私钥头", re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----")),
    ("疑似令牌", re.compile(rb"\b(?:ghp_|gho_|ghs_|github_pat_|pypi-AgEIcHlwaS5vcmc)[A-Za-z0-9_-]{8,}")),
)

# 行内豁免标记。**只对写着它的那一行生效**，不向文件或目录扩散。
ALLOW_MARK = "release-scan:allow"

# 读不出来的东西一律跳过，并如实计入 skipped —— 跳过不等于干净。
_BINARY_SNIFF = 4096


class WordlistMissing(RuntimeError):
    """词表缺失或为空。扫描器拒绝在这种状态下给出任何结论。"""


class SelfTestFailed(RuntimeError):
    """自检未通过。扫描器本身不可信，本次结果全部作废。"""


def load_words(env: dict[str, str] | None = None) -> list[str]:
    """从环境载入词表。**空词表视为错误，不是「没有敏感词」。**"""
    env = os.environ if env is None else env
    words: list[str] = []

    raw = env.get("AIDUMEI_SCAN_WORDS", "")
    words += [w.strip() for w in raw.split("|") if w.strip()]

    path = env.get("AIDUMEI_SCAN_WORDLIST", "").strip()
    if path:
        p = Path(path)
        if not p.is_file():
            raise WordlistMissing(f"AIDUMEI_SCAN_WORDLIST 指向的文件不存在：{path}")
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                words.append(line)

    words = sorted(set(words))
    if not words:
        raise WordlistMissing(
            "词表为空。扫描器拒绝运行 —— 空词表会报出与「真的干净」无法区分的 0。\n"
            "请设置 AIDUMEI_SCAN_WORDS 或 AIDUMEI_SCAN_WORDLIST。"
        )
    return words


def scan_bytes(raw: bytes, words: list[str]) -> tuple[dict[str, int], dict[str, int]]:
    """扫一段字节，**逐行**判定豁免。

    返回 (命中, 已豁免)，两者都是 {标签: 次数}。
    次数是**出现次数**，不是文件数 —— 一个文件里出现 5 次就是 5。
    （旧扫描器在这里给的是文件数，汇报时把 5 说成 1，是个真实踩过的坑。）
    """
    hits: dict[str, int] = {}
    exempt: dict[str, int] = {}
    mark = ALLOW_MARK.encode()

    # 按行切分是安全的：UTF-8 的多字节序列里不可能出现 0x0A。
    for line in raw.split(b"\n"):
        bucket = exempt if mark in line else hits
        text = line.decode("utf-8", "ignore")

        for w in words:
            n = text.count(w)
            if n:
                bucket[w] = bucket.get(w, 0) + n

        for label, rx in PATTERNS:
            for m in rx.findall(line):
                s = m.decode("ascii", "ignore")
                if label == "公网IPv4" and _RESERVED.match(s):
                    continue
                key = f"{label}:{s}"
                bucket[key] = bucket.get(key, 0) + 1

    return hits, exempt


def scan_tree(root: str | Path, words: list[str]) -> tuple[
        dict[str, dict[str, int]], dict[str, dict[str, int]], int, int]:
    """递归扫一个目录。返回 (命中, 已豁免, 已扫文件数, 跳过数)。

    命中与已豁免的形状都是 {标签: {相对路径: 次数}}。
    """
    root = Path(root)
    found: dict[str, dict[str, int]] = {}
    waived: dict[str, dict[str, int]] = {}
    scanned = skipped = 0

    for p in root.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            skipped += 1
            continue
        if b"\x00" in raw[:_BINARY_SNIFF]:
            skipped += 1
            continue
        scanned += 1
        rel = str(p.relative_to(root))
        h, e = scan_bytes(raw, words)
        for k, n in h.items():
            found.setdefault(k, {})[rel] = n
        for k, n in e.items():
            waived.setdefault(k, {})[rel] = n

    return found, waived, scanned, skipped


def selftest(words: list[str]) -> None:
    """三向负向对照，全部成立才准出结果，否则整轮作废。

    正向：合成的脏样本**必须**被报出来 —— 防「扫描器坏了，什么都不报」。
    反向：合成的干净样本**必须**不被报出来 —— 防「扫描器疯了，什么都报」。
    豁免：标记只赦免它所在的那一行，**同文件其余行必须照样报警**
          —— 防豁免机制悄悄扩散成文件级/目录级后门。

    只验第一条是不够的：一个无条件返回「命中」的扫描器也能通过正向检查，
    但它会把所有真实结论淹没在假红里，同样等于没有扫描器。
    """
    probe = [*words, CANARY]

    with tempfile.TemporaryDirectory(prefix="aidumei-scan-selftest-") as td:
        (Path(td) / "dirty.txt").write_text(
            f"harmless preamble\n{CANARY}\ntrailing\n", encoding="utf-8")
        (Path(td) / "clean.txt").write_text(
            f"harmless preamble\n{CANARY_CLEAN}\ntrailing\n", encoding="utf-8")
        # 同一个文件里：第 1 行带标记应被豁免，第 2 行不带标记必须照报。
        (Path(td) / "waived.txt").write_text(
            f"{CANARY}  # {ALLOW_MARK} 自检夹具\n{CANARY}\n", encoding="utf-8")

        found, waived, scanned, _ = scan_tree(td, probe)

        if scanned != 3:
            raise SelfTestFailed(f"自检样本应扫到 3 个文件，实际 {scanned} 个 —— 遍历逻辑不可信")

        if CANARY not in found:
            raise SelfTestFailed(
                "自检失败：合成脏样本**没有**被报出来。扫描器坏了，本次「0 命中」不作数。")
        if "dirty.txt" not in found[CANARY]:
            raise SelfTestFailed("自检失败：命中定位到了错误的文件 —— 路径归属逻辑不可信")
        if "clean.txt" in found.get(CANARY, {}):
            raise SelfTestFailed(
                "自检失败：合成干净样本**被误报**。扫描器在制造假红，结论不可信。")

        if waived.get(CANARY, {}).get("waived.txt") != 1:
            raise SelfTestFailed("自检失败：行内豁免标记没生效，或没有被如实记账")
        if found.get(CANARY, {}).get("waived.txt") != 1:
            raise SelfTestFailed(
                "自检失败：豁免标记**溢出到了同文件的其他行**。"
                "这正是文件级/目录级豁免的病灶，绝不允许。")


def _hard_only(d: dict[str, dict[str, int]], words: list[str]) -> dict[str, dict[str, int]]:
    return {k: v for k, v in d.items()
            if k in words or any(k.startswith(p + ":") for p, _ in PATTERNS)}


def format_report(name: str, found: dict[str, dict[str, int]],
                  waived: dict[str, dict[str, int]], words: list[str],
                  scanned: int, skipped: int) -> tuple[str, int]:
    """渲染成人能读的报告。**永不只给一个总数** —— 逐词、逐文件都要摊开。

    一个坏掉的扫描器和一个干净的包，光看总数长得一模一样。
    """
    hard = _hard_only(found, words)
    ex = _hard_only(waived, words)
    total = sum(sum(v.values()) for v in hard.values())
    ex_total = sum(sum(v.values()) for v in ex.values())

    lines = [f"=== {name} ===",
             f"    已扫 {scanned} 个文件，跳过（二进制/符号链接/不可读）{skipped} 个",
             f"    硬敏感命中 = {total} 次，分布在 {len(hard)} 个词上"]
    if not hard:
        lines.append("    ✅ 无硬敏感命中")
    for k in sorted(hard):
        files = hard[k]
        lines.append(f"    ❌ {k} —— {sum(files.values())} 次 / {len(files)} 个文件")
        for f in sorted(files)[:10]:
            lines.append(f"         {f} ×{files[f]}")
        if len(files) > 10:
            lines.append(f"         …… 另有 {len(files) - 10} 个文件")

    # 豁免从「失败计数」里消失，但绝不从「视野」里消失。
    if ex:
        lines.append(f"    ⚪ 另有 {ex_total} 次已标注豁免（{ALLOW_MARK}），不计入失败：")
        for k in sorted(ex):
            for f in sorted(ex[k]):
                lines.append(f"         {k} @ {f} ×{ex[k][f]}")
    return "\n".join(lines), total


KNOWN_FLAGS = ("--selftest",)


def main(argv: list[str]) -> int:
    # 未知选项一律拒绝，不做「悄悄忽略」。
    # 原实现是 [a for a in argv if not a.startswith("-")]：`--name X` 的选项名被丢掉，
    # 它的值 X 却被当成扫描目录 —— 而 X 是个不存在的路径，scan_tree 扫出 0 个文件，
    # 报告照样打印「✅ 无硬敏感命中」。一个拼错的参数换来一行绿色，
    # 正是本工具要消灭的那类静默失败。
    unknown = [a for a in argv if a.startswith("-") and a not in KNOWN_FLAGS]
    if unknown:
        print(f"[拒绝运行] 未知选项：{' '.join(unknown)}；"
              f"本工具只接受 {' '.join(KNOWN_FLAGS)} 与若干目录路径。", file=sys.stderr)
        return 2

    targets = [a for a in argv if not a.startswith("-")]
    only_selftest = "--selftest" in argv

    try:
        words = load_words()
    except WordlistMissing as e:
        print(f"[拒绝运行] {e}", file=sys.stderr)
        return 2

    try:
        selftest(words)
    except SelfTestFailed as e:
        print(f"[自检失败] {e}", file=sys.stderr)
        return 3

    print(f"[自检通过] 三向对照成立（脏样本报警 / 干净样本不报 / 豁免不外溢）；"
          f"词表 {len(words)} 条（内容不回显）")
    print(f"[覆盖面] {' / '.join(SURFACES)}")
    if only_selftest:
        return 0

    if not targets:
        print("用法：release_scan.py <目录> [<目录> ...]", file=sys.stderr)
        return 2

    # 目标必须真实存在。扫一个不存在的路径得到的 0，
    # 与扫一个真的干净的目录得到的 0，在报告里长得一模一样。
    missing = [t for t in targets if not Path(t).exists()]
    if missing:
        print(f"[拒绝运行] 扫描目标不存在：{' '.join(missing)}；"
              f"不存在的目标会报出与「真的干净」无法区分的 0。", file=sys.stderr)
        return 2

    grand = 0
    for t in targets:
        found, waived, scanned, skipped = scan_tree(t, words)
        report, n = format_report(t, found, waived, words, scanned, skipped)
        print(report)
        grand += n

    print(f"\n总计硬敏感命中 = {grand} 次")
    return 1 if grand else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
