"""benchmarks.verify_data — 跑分数据落地校验（v20 · P0-8）

用户视角审计五实测：`data_manifest.json` 全盘 0 个、LoCoMo 数据文件全盘 0 个、
`bench_data` 目录不存在 —— 而报告 6.1 写着「LoCoMo 已下载 + 已锁哈希」。
审计原话：「要么文件在别的路径，要么报告的数字是拍的不是量的。」

根因不是「忘了下载」，是**清单里没有落地目录声明**：`data_manifest.json` 只记
`filename`，真正的目录约定只活在 `download.py` 的代码里。于是「数据在不在」这个
问题没有任何机器可回答的形式 —— 只能靠人去某台机器上 `ls` 一下，而人会记错路径。

这个模块让清单自己能回答这个问题。

铁律 14 的口径写死在这里：**显式指定但无效 → 直接报错点名坏路径，绝不回退。**
如果部署方设了 `AIDUMEI_BENCH_DATA_DIR` 却指向一个不存在的目录，那是配置错误，
必须报出来；悄悄退回默认目录、然后报告「数据不在」，会让人去查一个错误的方向。

用法：
    python -m benchmarks.verify_data           # 校验，缺什么点名什么
    python -m benchmarks.verify_data --json    # 机器可读输出

退出码：0 = 全部就位且哈希相符；1 = 有缺失或哈希不符（已逐条点名）；
2 = 拒绝运行（清单缺失/损坏，或显式指定的目录不存在）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data_manifest.json")


class ManifestError(Exception):
    """清单本身有问题 —— 与「数据不在」是两件事，退出码也不同。"""


def load_manifest(path: str | None = None) -> dict:
    """读清单。

    ⚠️ `path` 默认取 `None` 而不是直接写 `MANIFEST_PATH`：默认参数的值在 `def`
    执行的那一刻就被捕获了，之后再改模块级的 `MANIFEST_PATH` 对它毫无影响 ——
    调用方（测试、或想指向另一份清单的工具）会以为自己改生效了，实际读的还是老路径。
    这不是测试的便利问题，是一个会静默给出错误答案的绑定时机陷阱。
    """
    path = path or MANIFEST_PATH
    if not os.path.exists(path):
        raise ManifestError(f"清单不存在：{path}")
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ManifestError(f"清单读不出来：{path}（{e}）") from e
    if "_base_dir" not in m:
        raise ManifestError(
            f"清单里没有 _base_dir 声明：{path}。"
            "没有落地目录声明，「数据在不在」就没有机器可回答的形式 —— 这正是 P0-8 的缺陷本身"
        )
    return m


def resolve_base_dir(manifest: dict, env: dict | None = None) -> tuple[str, str]:
    """返回 (目录, 来源)。来源 ∈ {"env", "default"}。

    铁律 14：显式设了环境变量却指向不存在的目录 —— **抛错点名**，绝不回退默认。
    悄悄回退会让「配置写错了」伪装成「数据没下载」，把人引向错误的排查方向。
    """
    env = os.environ if env is None else env
    decl = manifest["_base_dir"]
    var = decl.get("env_var") or ""
    raw = (env.get(var) or "").strip() if var else ""
    if raw:
        path = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isdir(path):
            raise ManifestError(
                f"{var} 显式指向 {path}，但该目录不存在。"
                "按纪律这里不回退默认目录 —— 否则「配置写错了」会伪装成「数据没下载」"
            )
        return path, "env"
    return os.path.abspath(os.path.expanduser(decl.get("default") or ".")), "default"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(manifest: dict | None = None, env: dict | None = None) -> dict:
    """逐个数据集回答「在／不在／哈希对不对」。"""
    m = manifest if manifest is not None else load_manifest()
    base, source = resolve_base_dir(m, env)
    datasets, missing, mismatched, ok = {}, [], [], []
    for name, spec in m.items():
        if name.startswith("_") or not isinstance(spec, dict):
            continue
        fn = spec.get("filename")
        if not fn:
            missing.append({"dataset": name, "reason": "清单里没有 filename"})
            continue
        path = os.path.join(base, fn)
        row = {"dataset": name, "filename": fn, "path": path}
        if not os.path.exists(path):
            row["status"] = "missing"
            missing.append(row)
        else:
            actual = _sha256(path)
            expect = spec.get("sha256")
            row["size_bytes"] = os.path.getsize(path)
            if expect and actual != expect:
                row["status"] = "hash_mismatch"
                row["expected_sha256"] = expect
                row["actual_sha256"] = actual
                mismatched.append(row)
            else:
                row["status"] = "ok"
                ok.append(row)
        datasets[name] = row
    return {
        "base_dir": base, "base_dir_source": source,
        "datasets": datasets, "ok": ok, "missing": missing, "mismatched": mismatched,
        "all_present": not missing and not mismatched,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="跑分数据落地校验（P0-8）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)
    try:
        rep = verify()
    except ManifestError as e:
        print(f"[拒绝运行] {e}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(rep, sys.stdout, ensure_ascii=False, indent=1)
        print()
    else:
        print(f"落地目录：{rep['base_dir']}（来源：{rep['base_dir_source']}）")
        for row in rep["ok"]:
            print(f"  ✅ {row['dataset']:<14} {row['filename']} "
                  f"({row['size_bytes']} 字节，哈希相符)")
        for row in rep["missing"]:
            print(f"  ❌ {row['dataset']:<14} 缺失：{row.get('path', row.get('reason'))}")
        for row in rep["mismatched"]:
            print(f"  ❌ {row['dataset']:<14} 哈希不符：{row['path']}")
            print(f"      期望 {row['expected_sha256'][:16]}… "
                  f"实际 {row['actual_sha256'][:16]}…")
        if rep["all_present"]:
            print("全部就位。")
        else:
            print(f"\n缺失 {len(rep['missing'])} 个，哈希不符 {len(rep['mismatched'])} 个。"
                  f"\n跑分闸门按纪律不许在这个状态下打开 —— 数据不在，闸门就是空转。")
    return 0 if rep["all_present"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
