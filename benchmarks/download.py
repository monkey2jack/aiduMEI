"""benchmarks.download — 数据集登记与哈希锁定（数据本体不进仓库）。

数据集存放在仓库之外：``AIDUMEI_BENCH_DATA_DIR``（默认
``~/.aidumem/bench_data``）。本脚本不做网络下载——LongMemEval 官方数据
经 Google Drive / Hugging Face 分发，LoCoMo 在官方仓库内，直连脚本极易
腐坏且绕过许可证页面。正确姿势：

1. 按 PROTOCOL.md 的官方来源手工获取数据文件；
   - LongMemEval（MIT）: github.com/xiaowu0162/LongMemEval
   - LoCoMo（CC BY-NC 4.0，仅限非商业评测用途）: github.com/snap-research/locomo
2. 放进数据目录后执行 ``python -m benchmarks.download --register <dataset> <文件名>
   --source-commit <上游提交号>``；
3. 脚本校验 schema、计算 SHA-256、记下上游提交号，写入
   ``benchmarks/data_manifest.json``；
4. formal 运行前 run.py 会拿实际哈希与 manifest 对账，对不上拒绝开跑；
   提交号缺失或仍是 PENDING 同样拒绝开跑。

**为什么光有哈希还不够**：SHA-256 只能证明「我这次跑的就是这个文件」，
证明不了「这个文件是从上游哪一版取的」。而两个数据集的标注都在持续修——
LoCoMo 修过 evidence 引用，LongMemEval 换过 oracle 切分。别人拿着我们公布的
哈希，如果手里的文件对不上，没有提交号就无从判断是「取错了版本」还是
「文件被改过」。提交号让第三方能把上游检出到同一版再自证哈希。

manifest 进仓库（锁定即承诺）；数据文件永远不进仓库。
LoCoMo 的图片只有外部 URL：本管线**不抓取、不缓存**任何图片。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time

MANIFEST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data_manifest.json"
)

DATASETS = ("longmemeval", "locomo")

# 上游提交号：短号也认（7 位起），但必须是十六进制。不接受 tag 名、分支名——
# 那两样都会移动，移动的东西不能当先验承诺。
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")


def normalize_source_commit(value: str | None) -> str:
    """把 --source-commit 规范成小写十六进制；空/PENDING/非法一律拒绝登记。

    这里宁可拦死也不给默认值。给个 "unknown" 之类的占位符，manifest 就会
    带着一个看起来已登记、实则没有溯源的条目进仓库——而 formal 闸门只会
    检查「非空且非 PENDING」，占位符正好能骗过它。
    """
    raw = (value or "").strip()
    if not raw or "PENDING" in raw.upper():
        raise SystemExit(
            "登记被拒：--source-commit 不能为空，也不能是 PENDING。\n"
            "  数据文件自身没有版本号，上游标注却在持续修；只锁哈希只能证明\n"
            "  「这次跑的就是这个文件」，证明不了「它取自哪一版」。\n"
            "  取法：在数据集官方仓库的检出目录里跑 `git rev-parse HEAD`。"
        )
    low = raw.lower()
    if not _COMMIT_RE.match(low):
        raise SystemExit(
            f"登记被拒：--source-commit 不像 git 提交号: {raw!r}\n"
            "  要 7–40 位十六进制。tag 名和分支名一律不收——它们会移动，\n"
            "  会移动的标识当不了先验承诺。"
        )
    return low


def data_dir() -> str:
    return os.environ.get(
        "AIDUMEI_BENCH_DATA_DIR",
        os.path.expanduser(os.path.join("~", ".aidumem", "bench_data")),
    )


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def register(dataset: str, filename: str, source_commit: str | None = None) -> dict:
    """校验 + 计算哈希 + 记提交号 + 写 manifest。schema 不合格不许登记。"""
    if dataset not in DATASETS:
        raise SystemExit(f"未知数据集: {dataset}（可选: {', '.join(DATASETS)}）")
    commit = normalize_source_commit(source_commit)
    path = os.path.join(data_dir(), filename)
    if not os.path.exists(path):
        raise SystemExit(
            f"找不到数据文件: {path}\n"
            f"请先按 PROTOCOL.md 从官方来源获取并放入 {data_dir()}"
        )

    with open(path, encoding="utf-8") as f:
        payload = json.load(f)

    from benchmarks.schemas import validate_locomo, validate_longmemeval

    if dataset == "longmemeval":
        report = validate_longmemeval(payload, expect_total=500)
    else:
        report = validate_locomo(payload, expect_samples=10)

    digest = _sha256_file(path)
    manifest: dict = {}
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            manifest = json.load(f)
    manifest[dataset] = {
        "filename": filename,
        "sha256": digest,
        "source_commit": commit,
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "size_bytes": os.path.getsize(path),
        "schema_report": report,
    }
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
    return manifest[dataset]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="benchmarks.download",
        description="登记数据集文件并把 SHA-256 锁进 manifest（不做网络下载）",
    )
    parser.add_argument("--register", nargs=2, metavar=("DATASET", "FILENAME"),
                        help="校验并登记一个数据文件")
    parser.add_argument("--source-commit", metavar="SHA",
                        help="该数据文件取自上游仓库的哪个提交（7–40 位十六进制，"
                             "登记必填；见 PROTOCOL.md §1）")
    parser.add_argument("--show", action="store_true", help="打印当前 manifest")
    args = parser.parse_args(argv)

    if args.show:
        if os.path.exists(MANIFEST_PATH):
            with open(MANIFEST_PATH, encoding="utf-8") as f:
                print(f.read())
        else:
            print("（尚无 manifest——还没有登记任何数据集）")
        return 0
    if args.register:
        entry = register(*args.register, source_commit=args.source_commit)
        print(json.dumps(
            {k: v for k, v in entry.items() if k != "schema_report"},
            ensure_ascii=False, indent=2,
        ))
        report = entry["schema_report"]
        anomalies = report.get("anomalies") or []
        print(f"schema 校验通过；统计: "
              f"{json.dumps({k: v for k, v in report.items() if k != 'anomalies'}, ensure_ascii=False)}")
        if anomalies:
            print(f"⚠ 上游数据可疑点 {len(anomalies)} 处（如实上报，未修改数据）：",
                  file=sys.stderr)
            for a in anomalies[:20]:
                print(f"  - {a}", file=sys.stderr)
            if len(anomalies) > 20:
                print(f"  …… 其余 {len(anomalies) - 20} 处见 manifest 的 schema_report",
                      file=sys.stderr)
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
