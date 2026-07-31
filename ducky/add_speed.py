#!/usr/bin/env python3
"""
aiduMEM /add 高速路径 — v9.1 "Mnemosyne" · 谟涅摩绪涅（兼容门面）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
真源已迁至 `ducky.speed.*` 子包；本文件只做 re-export，旧 import 路径不变。

  ducky.speed.config    配置 / messages_to_text
  ducky.speed.cache     抽取缓存
  ducky.speed.fastpath  短文本快路径
  ducky.speed.jobs      异步 job
  ducky.speed.stats     潮浪命中统计
  ducky.speed.coalesce  会话合并队列
  ducky.speed.patch     LLM 补丁
  ducky.speed.pipeline  run_add_pipeline
"""
from ducky.speed import *  # noqa: F401,F403
from ducky.speed import __all__ as __all__  # noqa: F401
