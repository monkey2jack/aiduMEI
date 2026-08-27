"""ducky.engine_mode — 引擎档位选择（v20.2.3 · 用户可选三档）

**自动挡好，但不该是唯一选项。** 部署形态不同，最优解也不同：

    auto （自动挡·默认）  云腿为主，断供自动切本地备胎，恢复自动升挡。
                          代价：本地嵌入模型常驻（实测 +151MB RSS）。
    cloud（云端档）        只用云腿。不装/不加载本地模型，不写本地索引 ——
                          **退回自动挡之前的体量**（省下那 151MB 与
                          169MB 磁盘）。代价：云断供时没有备胎，
                          召回如实判 degraded。
    local（本地档）        只用本地腿。零 token、零外部网络、零密钥：
                          确定性抽取 + 本地向量 + 全文检索。
                          代价：没有 LLM 蒸馏的语义精修，排序品质不如云挡。

设计纪律：
  - **档位是部署方的选择，不是运行时的猜测**。本模块只读配置、不做判定；
    「云腿现在能不能用」由熔断器（ducky.gear）判，两件事不许混。
  - 非法值回退 auto 并出声（配置雷纪律，见 ducky.env_config）——
    档位配错不该让服务起不来。
  - 两条腿的开关是**独立谓词**（cloud_leg_enabled / local_leg_enabled），
    不是一个三选一的 if-else 链：调用点只问「我这条腿开着吗」，
    将来加第三种形态也不必改调用点。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("aiduMEM.engine_mode")

_ENV = "AIDUMEI_ENGINE_MODE"
_VALID = ("auto", "cloud", "local")
_DEFAULT = "auto"
_warned: set = set()


def configured_mode() -> str:
    """部署方选定的档位：auto | cloud | local。非法值回退 auto 并告警一次。"""
    raw = (os.environ.get(_ENV) or "").strip().lower()
    if not raw:
        return _DEFAULT
    if raw in _VALID:
        return raw
    if raw not in _warned:
        logger.warning("⚙️ %s 非法值 %r，已回退默认 %s（可选：%s）",
                       _ENV, raw, _DEFAULT, "/".join(_VALID))
        _warned.add(raw)
    return _DEFAULT


def cloud_leg_enabled() -> bool:
    """云腿（云嵌入 / LLM 蒸馏 / 云向量库）是否允许使用。"""
    return configured_mode() != "local"


def local_leg_enabled() -> bool:
    """本地腿（本地嵌入模型 / 本地向量库）是否允许使用。

    False 时：模型不加载（省 151MB）、本地索引不写、降挡无备胎可切。
    """
    return configured_mode() != "cloud"


def mode_status() -> dict:
    """/health 探针：档位与两条腿的开关，运维面一眼可见。"""
    m = configured_mode()
    return {
        "configured": m,
        "cloud_leg": cloud_leg_enabled(),
        "local_leg": local_leg_enabled(),
        "note": {
            "auto": "自动挡：云腿为主，断供自动切本地备胎",
            "cloud": "云端档：只用云腿，本地模型不加载（省内存，断供无备胎）",
            "local": "本地档：只用本地腿，零 token 零外部网络",
        }[m],
    }


def reset_mode_warnings_for_tests() -> None:
    _warned.clear()
