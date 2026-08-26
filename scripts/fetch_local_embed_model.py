#!/usr/bin/env python3
"""scripts/fetch_local_embed_model.py — 部署期取模（v20.2 自动挡 WP-E）

把本地嵌入备胎的模型文件放进缓存目录。运行时零网络是备胎的硬纪律，
所以「联网」这件事只允许发生在这里（部署期、人在场、可走代理）。

用法：
  python3 scripts/fetch_local_embed_model.py            # 联网下载（可 HTTPS_PROXY）
  python3 scripts/fetch_local_embed_model.py --from DIR # 离线：从打包目录拷入
    （生产机外网受限时的路数：在能联网的机器跑一次本脚本，把缓存目录
     打包传过去，再 --from 指入。v20.2 阶段 0 实测：pypi.org 生产不通、
     镜像装依赖 + 模型直传是可行路径。）
"""
import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ducky.local_embed import LOCAL_EMBED_MODEL, local_embed_cache_dir  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", default=None,
                    help="离线模式：从已打包的缓存目录拷入（含 models--Qdrant--* 子目录）")
    args = ap.parse_args()
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

    os.environ["HF_HUB_OFFLINE"] = "1"
    from ducky.local_embed import is_local_embed_available, reset_local_embed_for_tests
    reset_local_embed_for_tests()
    ok = is_local_embed_available()
    print("离线自检:", "✅ 备胎可用" if ok else "❌ 模型未就绪")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
