#!/usr/bin/env python3
"""scripts/fetch_local_embed_model.py — 部署期取模（v20.2 自动挡 WP-E）

把本地嵌入备胎的模型文件放进缓存目录。运行时零网络是备胎的硬纪律，
所以「联网」这件事只允许发生在这里（部署期、人在场、可走代理）。

v20.4.0（P1-9，外审 Qwen C1）：取模后按仓内清单
scripts/local_embed_model_sha256.json 逐文件校验 sha256——
部署期是供应链信任点，拉下来就信等于把完整性交给传输层运气。
口径：不匹配 → 删除该文件、报错、非 0 退出；清单标 "unverified"
的条目 → WARNING 跳过、不阻塞（清单是从一台真机的下载产物钉出来的，
换个 fastembed/huggingface_hub 版本可能多个别文件，不许因此把新用户
挡在门外；但警告必须响亮，部署者要看得见哪些文件没有哈希背书）。

用法：
  python3 scripts/fetch_local_embed_model.py            # 联网下载（可 HTTPS_PROXY）
  python3 scripts/fetch_local_embed_model.py --from DIR # 离线：从打包目录拷入
    （生产机外网受限时的路数：在能联网的机器跑一次本脚本，把缓存目录
     打包传过去，再 --from 指入。v20.2 阶段 0 实测：pypi.org 生产不通、
     镜像装依赖 + 模型直传是可行路径。）
"""
import argparse
import hashlib
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ducky.local_embed import LOCAL_EMBED_MODEL, local_embed_cache_dir  # noqa: E402

#: 仓内 sha256 清单，与本脚本同目录。清单格式见该文件 _comment 字段。
DEFAULT_MANIFEST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "local_embed_model_sha256.json")

#: 清单里「钉不了」的占位值：跳过校验 + WARNING，不阻塞部署。
UNVERIFIED = "unverified"


def sha256_file(path: str) -> str:
    """流式算单文件 sha256（模型 blob 近百 MB，不一次读进内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(manifest_path: str = DEFAULT_MANIFEST_PATH) -> dict:
    """读清单文件。清单自身缺失/坏掉是部署事故：直接抛，不许静默放行。"""
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    if not isinstance(manifest.get("files"), dict):
        raise ValueError(f"清单缺少 files 字段：{manifest_path}")
    return manifest


def verify_model_files(cache_dir: str, manifest: dict):
    """按清单逐文件校验缓存目录，返回 (errors, warnings)。

    - 匹配：放行，文件原样保留；
    - 不匹配：当场删除该文件并记 error（坏文件不许留在缓存里被加载）；
    - 清单钉了但磁盘上没有：记 error（模型不可能完整）；
    - "unverified"：记 warning 并跳过——诚实承认这条没有哈希背书。
    """
    errors, warnings = [], []
    for rel, expect in manifest["files"].items():
        path = os.path.join(cache_dir, rel)
        if expect == UNVERIFIED:
            warnings.append(f"WARNING: {rel} 清单标 unverified，跳过 sha256 校验")
            continue
        if not os.path.isfile(path):
            errors.append(f"清单钉了的文件不存在：{rel}")
            continue
        actual = sha256_file(path)
        if actual != expect:
            os.remove(path)
            errors.append(
                f"sha256 不匹配：{rel}（期望 {expect[:16]}… 实得 {actual[:16]}…），"
                "已删除该文件")
    return errors, warnings


def verify_staged_model(cache_dir: str,
                        manifest_path: str = DEFAULT_MANIFEST_PATH) -> bool:
    """取模后的完整校验闸：打印结果，返回 True 才许继续走自检。"""
    manifest = load_manifest(manifest_path)
    errors, warnings = verify_model_files(cache_dir, manifest)
    for w in warnings:
        print(w)
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)
    if errors:
        print(f"模型文件校验失败（{len(errors)} 处）——缓存目录里的模型不可信，"
              "请重新取模或检查来源。", file=sys.stderr)
        return False
    pinned = sum(1 for v in manifest["files"].values() if v != UNVERIFIED)
    if pinned == 0:
        print("WARNING: 清单里没有任何已钉住的文件，本次取模没有 sha256 背书。")
    else:
        print(f"sha256 校验通过：{pinned} 个文件逐一匹配清单。")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", default=None,
                    help="离线模式：从已打包的缓存目录拷入（含 models--Qdrant--* 子目录）")
    args = ap.parse_args(argv)
    dest = local_embed_cache_dir()
    os.makedirs(dest, exist_ok=True)

    if args.src:
        copied = 0
        for name in os.listdir(args.src):
            if name.startswith("models--"):
                target = os.path.join(dest, name)
                if os.path.exists(target):
                    shutil.rmtree(target)
                shutil.copytree(os.path.join(args.src, name), target)
                copied += 1
        print(f"离线拷入 {copied} 个模型目录 → {dest}")
    else:
        os.environ.pop("HF_HUB_OFFLINE", None)  # 部署期显式允许联网
        from fastembed import TextEmbedding
        TextEmbedding(LOCAL_EMBED_MODEL, cache_dir=dest)
        print(f"已下载 {LOCAL_EMBED_MODEL} → {dest}")

    # 完整性闸在自检之前：校验不过就不必再尝试加载，直接非 0 退出。
    if not verify_staged_model(dest):
        return 1

    os.environ["HF_HUB_OFFLINE"] = "1"
    from ducky.local_embed import is_local_embed_available, reset_local_embed_for_tests
    reset_local_embed_for_tests()
    ok = is_local_embed_available()
    print("离线自检:", "✅ 备胎可用" if ok else "❌ 模型未就绪")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
