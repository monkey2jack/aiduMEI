"""tests/test_v20_4_model_hash.py — v20.4.0 P1-9：部署期模型文件 sha256 校验

外审 Qwen C1 实锤：scripts/fetch_local_embed_model.py 全程无哈希校验，
是部署期供应链信任点——模型从 HuggingFace 拉下来就信，中间被换一字节
也照单全收。v20.4.0 在取模后按仓内清单
（scripts/local_embed_model_sha256.json）逐文件校验 sha256。

本用例组钉死三条口径：
  1. 匹配 → 放行；
  2. 篡改一字节 → 拒收（坏文件当场删除）、主流程退出码非 0；
  3. 清单里标 "unverified" 的条目 → WARNING 跳过，不阻塞部署
     （清单钉不了的文件不许假装覆盖，但也不许把新用户挡在门外）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os

# 与 tests/test_v20_3_e2e_smoke.py 同一路数：按文件路径加载脚本模块，
# 不依赖 scripts 是不是 package。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "fetch_local_embed_model",
    os.path.join(_ROOT, "scripts", "fetch_local_embed_model.py"),
)
fem = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fem)


def _write(path, data: bytes):
    """在假缓存里落一个文件，顺手建父目录。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _make_cache(tmp_path, files: dict) -> str:
    """按 {相对路径: 内容} 造一个假模型缓存目录，返回缓存根。"""
    root = os.path.join(str(tmp_path), "cache")
    for rel, data in files.items():
        _write(os.path.join(root, rel), data)
    return root


def _manifest_for(files: dict) -> dict:
    """按 {相对路径: 内容} 生成一份全匹配的清单（sha256 现算）。"""
    return {
        "_comment": "测试清单",
        "model": "BAAI/bge-small-zh-v1.5",
        "files": {rel: hashlib.sha256(data).hexdigest()
                  for rel, data in files.items()},
    }


_FAKE_FILES = {
    "models--Qdrant--bge-small-zh-v1.5/blobs/aaa": b"fake-onnx-weights",
    "models--Qdrant--bge-small-zh-v1.5/blobs/bbb": b"fake-tokenizer",
    "models--Qdrant--bge-small-zh-v1.5/refs/main": b"fake-revision",
}


def test_matching_manifest_passes(tmp_path):
    """逐字节匹配：校验通过，无错误无警告，文件原样保留。"""
    cache = _make_cache(tmp_path, _FAKE_FILES)
    errors, warnings = fem.verify_model_files(cache, _manifest_for(_FAKE_FILES))
    assert errors == []
    assert warnings == []
    # 放行的文件一个字节都不许动
    for rel, data in _FAKE_FILES.items():
        with open(os.path.join(cache, rel), "rb") as f:
            assert f.read() == data


def test_tampered_file_is_rejected_and_deleted(tmp_path):
    """篡改一字节：报错、坏文件当场删除。"""
    cache = _make_cache(tmp_path, _FAKE_FILES)
    victim = "models--Qdrant--bge-small-zh-v1.5/blobs/aaa"
    with open(os.path.join(cache, victim), "r+b") as f:
        f.write(b"X")  # 改掉第一个字节
    errors, warnings = fem.verify_model_files(cache, _manifest_for(_FAKE_FILES))
    assert errors, "被篡改的文件必须被检出"
    assert any(victim in e for e in errors)
    assert not os.path.exists(os.path.join(cache, victim)), \
        "校验不过的文件必须删除，不许留在缓存里被加载"


def test_missing_file_is_an_error(tmp_path):
    """清单钉了的文件在磁盘上不存在：同样是校验失败（模型不可能完整）。"""
    cache = _make_cache(tmp_path, _FAKE_FILES)
    os.remove(os.path.join(
        cache, "models--Qdrant--bge-small-zh-v1.5/refs/main"))
    errors, _ = fem.verify_model_files(cache, _manifest_for(_FAKE_FILES))
    assert errors


def test_unverified_entry_warns_and_skips(tmp_path):
    """清单标 unverified 的条目：跳过校验、给 WARNING、不阻塞。"""
    cache = _make_cache(tmp_path, _FAKE_FILES)
    manifest = _manifest_for(_FAKE_FILES)
    unverified_rel = "models--Qdrant--bge-small-zh-v1.5/blobs/bbb"
    manifest["files"][unverified_rel] = "unverified"
    # 把这个文件改坏——unverified 条目不该被校验，所以照样放行
    with open(os.path.join(cache, unverified_rel), "wb") as f:
        f.write(b"tampered-but-unverified")
    errors, warnings = fem.verify_model_files(cache, manifest)
    assert errors == []
    assert warnings, "unverified 条目必须留下 WARNING，不许静默"
    assert any(unverified_rel in w for w in warnings)


def test_all_unverified_warns_but_does_not_block(tmp_path):
    """全部条目都 unverified：WARNING 照旧，但校验结论仍是放行。

    新用户机器上清单若一条都钉不上，不许把人挡在门外——但警告必须响亮，
    部署者要看得见「这次取模没有任何哈希背书」。
    """
    cache = _make_cache(tmp_path, _FAKE_FILES)
    manifest = _manifest_for(_FAKE_FILES)
    for rel in manifest["files"]:
        manifest["files"][rel] = "unverified"
    errors, warnings = fem.verify_model_files(cache, manifest)
    assert errors == []
    assert len(warnings) >= len(_FAKE_FILES)


def test_main_rejects_tampered_offline_copy(tmp_path, monkeypatch):
    """端到端走 main()：--from 拷入被篡改的模型，退出码必须非 0。

    用仓内真清单（scripts/local_embed_model_sha256.json）里钉过的真实
    相对路径造一个内容错误的假文件——拷入后校验必炸，main 必须在
    自检（加载 fastembed）之前就返回非 0。
    """
    manifest_path = os.path.join(
        _ROOT, "scripts", "local_embed_model_sha256.json")
    with open(manifest_path, encoding="utf-8") as f:
        real_manifest = json.load(f)
    pinned = [rel for rel, digest in real_manifest["files"].items()
              if digest != "unverified"]
    assert pinned, "仓内清单必须至少钉住一个文件，本用例才有意义"

    src = os.path.join(str(tmp_path), "src")
    _write(os.path.join(src, pinned[0]), b"definitely-not-the-real-model")
    dest = os.path.join(str(tmp_path), "dest")
    monkeypatch.setenv("AIDUMEI_LOCAL_EMBED_CACHE", dest)

    rc = fem.main(["--from", src])
    assert rc != 0, "篡改的模型文件拷入后，取模脚本必须非 0 退出"
    assert not os.path.exists(os.path.join(dest, pinned[0])), \
        "被拒收的文件不许留在缓存目录里"
