"""ducky.local_embed — 本地嵌入备胎（v20.2 WP-E · 智慧引擎自动挡）

云端嵌入服务失效时的本地推理腿：fastembed（纯 ONNX，无深度学习框架），
模型 BAAI/bge-small-zh-v1.5（512 维 · 91MB）。选型依据是阶段 0 POC 双环境
实测（2026-08-26）：中文语义 sanity 双 6/6，单条延迟 1.0ms（开发机）/
6.7ms（生产 2 核 x86），离线加载 0.3s。

三条纪律：
  1. **备胎不许伸手要网**：进程内强制 HF_HUB_OFFLINE —— 模型文件必须
     部署期就位（scripts/fetch_local_embed_model.py），运行时零网络。
     一个「故障时才需要的备胎」若在故障时才去联网下模型，是语义矛盾。
  2. **可用性探测不抛、推理失败必抛**：is_local_embed_available() 给
     切换器做无副作用探测；local_embed_texts() 失败抛明确异常，由调用方
     决定降级（与三态判语同一哲学：失败要可见，不许静默变空）。
  3. **本地向量与云向量是两种语言**（512 维 vs 1024 维）：本模块产出的
     向量只许进本地 collection（dual_index），绝不许混入云向量池。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import List, Optional

logger = logging.getLogger("aiduMEM.local_embed")

LOCAL_EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
LOCAL_EMBED_DIM = 512
_CACHE_ENV = "AIDUMEI_LOCAL_EMBED_CACHE"

try:
    from fastembed import TextEmbedding  # noqa: F401
    _FASTEMBED_IMPORTABLE = True
except ImportError:
    TextEmbedding = None  # type: ignore[assignment]
    _FASTEMBED_IMPORTABLE = False

_model = None
_model_lock = threading.Lock()
_load_error: Optional[str] = None


def local_embed_cache_dir() -> str:
    """模型缓存目录：env 显式指定优先，默认用户缓存目录（不进 DATA_DIR ——
    模型是可再生的部署产物，不是用户数据，不该被数据备份/生命线背走）。"""
    raw = os.environ.get(_CACHE_ENV)
    if raw:
        return os.path.expanduser(raw.strip())
    return os.path.expanduser("~/.cache/aidumei/fastembed")


def _load_model():
    global _model, _load_error
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        if not _FASTEMBED_IMPORTABLE:
            raise RuntimeError(
                "本地嵌入不可用：fastembed 未安装（可选依赖组 local-embed；"
                "pip install 'aidumei[local-embed]' 或 pip install fastembed）"
            )
        # 备胎不许伸手要网：只认已就位的模型文件。强制覆写而非 setdefault
        # （v20.2.1 外审 Y1：进程环境预置 HF_HUB_OFFLINE=0 可绕过 setdefault）
        # —— 改进程环境是刻意的：本进程内任何 HF 下载通道都该被封死，
        # 部署期联网下载有专门通道（scripts/fetch_local_embed_model.py）。
        os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            _model = TextEmbedding(LOCAL_EMBED_MODEL, cache_dir=local_embed_cache_dir())
        except Exception as exc:
            _load_error = str(exc)[:200]
            raise RuntimeError(
                f"本地嵌入模型加载失败（缓存目录 {local_embed_cache_dir()}，"
                f"运行时禁网下载 —— 请先跑 scripts/fetch_local_embed_model.py "
                f"部署模型文件）：{_load_error}"
            ) from exc
        logger.info("🪫 本地嵌入备胎就绪：%s（%d 维，缓存 %s）",
                    LOCAL_EMBED_MODEL, LOCAL_EMBED_DIM, local_embed_cache_dir())
        return _model


def is_local_embed_available() -> bool:
    """无副作用可用性探测：依赖可导入 + 模型能加载（懒加载后缓存判定）。
    探测失败绝不抛 —— 它是切换器和 /health 的眼睛，眼睛不能自己先瞎。

    v20.2.3：**云端档下直接报不可用** —— 这是省下那 151MB 的闸门本身。
    实测：onnxruntime 库 75MB + 模型会话 122MB，旋钮级调优（线程数/
    arena 策略/malloc_trim）全部无效（206~215MB 噪声内），模型也已是
    fastembed 目录里最小的中文可用款。**唯一有效的优化就是不加载它。**
    """
    from ducky.engine_mode import local_leg_enabled
    if not local_leg_enabled():
        return False
    if not _FASTEMBED_IMPORTABLE:
        return False
    if _model is not None:
        return True
    try:
        _load_model()
        return True
    except Exception:
        return False


def local_embed_status() -> dict:
    """/health 探针用：依赖在场性、模型就绪度、失败原因。"""
    return {
        "dependency": _FASTEMBED_IMPORTABLE,
        "model_loaded": _model is not None,
        "model": LOCAL_EMBED_MODEL,
        "dim": LOCAL_EMBED_DIM,
        "load_error": _load_error,
    }


def local_embed_texts(texts: List[str]) -> List[List[float]]:
    """本地推理。失败抛 RuntimeError（原因点名），绝不静默返回空。"""
    model = _load_model()
    return [list(v) for v in model.embed(list(texts))]


def reset_local_embed_for_tests() -> None:
    """测试用：清单例与错误态。"""
    global _model, _load_error
    with _model_lock:
        _model = None
        _load_error = None
