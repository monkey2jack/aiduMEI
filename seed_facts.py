#!/usr/bin/env python3
"""seed_facts.py — 为脱敏演示库补齐知识树事实（facts/entities）

用法: python seed_facts.py
      AIDUMEM_API_BASE=http://127.0.0.1:8767 python seed_facts.py
"""
import json
import os
import sys
import time
from urllib.parse import quote

import requests

BASE = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/")

FACTS = [
    # 人物
    ("人物", "张伟职业", "张伟是资深产品经理，常驻北京，在互联网行业工作八年"),
    ("人物", "张伟偏好", "张伟习惯每天早上喝一杯美式咖啡"),
    ("人物", "张伟家人", "张伟有一个女儿小名叫糖糖，今年五岁"),
    ("人物", "李娜职业", "李娜是一名前端工程师，负责数据可视化"),
    ("人物", "李娜偏好", "李娜周末喜欢去郊外爬山"),
    ("人物", "王强职业", "王强是后端架构师，深耕分布式系统"),
    ("人物", "赵敏角色", "赵敏是 AI 训练数据标注团队组长"),
    ("人物", "Dudu角色", "Dudu 是用户的 AI 记忆助手，负责事实沉淀与召回"),
    # 技术栈
    ("技术栈", "向量存储", "项目使用 Qdrant 作为向量存储引擎"),
    ("技术栈", "Embedding", "记忆提取使用 bge-m3 模型做文本嵌入"),
    ("技术栈", "大模型", "事实抽取调用 DeepSeek-V3.2 大模型"),
    ("技术栈", "重排器", "检索召回使用 SiliconFlow 重排器提升精度"),
    ("技术栈", "全文检索", "全文索引基于 FTS5 提供关键词召回"),
    # 项目
    ("项目", "记忆引擎", "aiduMEI 是面向个人知识沉淀的记忆引擎"),
    ("项目", "项目代号", "当前版本代号为 Zeus，主打多用户联邦记忆"),
    ("项目", "知识图谱", "记忆通过实体链接形成可追溯的知识图谱"),
    ("项目", "联邦同步", "多设备间通过联邦协议同步事实与信任分"),
    # 习惯偏好
    ("偏好", "代码风格", "团队成员偏好 Python 与 TypeScript 双栈开发"),
    ("偏好", "协作方式", "团队使用飞书文档与晨会同步进度"),
    ("偏好", "咖啡文化", "办公室常备意式浓缩与燕麦奶"),
    ("偏好", "周末活动", "团队每季度组织一次户外团建"),
    # 规划
    ("规划", "版本计划", "下一版本计划支持语音速记入口"),
    ("规划", "开源计划", "记忆引擎计划开源并公开架构文档"),
    ("规划", "安全合规", "数据脱敏与本地优先是发布前硬性要求"),
    # 技术细节
    ("技术细节", "Coalesce", "短时间内的多条记忆会合并为一次 LLM 调用"),
    ("技术细节", "信任分机制", "每条事实带信任分，随反馈提升或衰减"),
    ("技术细节", "记忆分层", "事实按 L0 铁律 / L1 习惯 / L2 语义分层存储"),
    ("技术细节", "衰减机制", "低相关事实按记忆曲线自动衰减过期"),
]

def add_fact(category: str, key: str, value: str):
    url = f"{BASE}/facts/add"
    params = {
        "category": category,
        "fact_key": key,
        "fact_value": value,
        "source": "demo",
    }
    qs = "&".join(f"{quote(k)}={quote(v)}" for k, v in params.items())
    try:
        r = requests.post(f"{url}?{qs}", timeout=30)
        return r.status_code, r.text[:80]
    except Exception as exc:
        return -1, str(exc)


def main():
    ok = fail = 0
    for category, key, value in FACTS:
        status, body = add_fact(category, key, value)
        if status == 200 and '"status":"ok"' in body:
            ok += 1
            print(f"[OK] {category}/{key}")
        else:
            fail += 1
            print(f"[FAIL] {category}/{key} → {status} {body}")
    print(f"\n== done: ok={ok} fail={fail} ==")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
