"""f0.6+++ rankfix · 原话 vs 蒸馏事实排序回归(native 实机缺陷:长原话压过精炼事实)。

10 个真实风格话题:每个话题 1 条蒸馏事实(lane=distill,SessionEnd 蒸馏同形)+ 3 条
很长的逐轮原话(ingest 钩子同形,source=cc-ingest,native 存为 lane=raw)。按 CC inject
的口径 /search limit 3,要求蒸馏事实 10/10 进前 3,且至少 8/10 排第 1(修复前离线只有 3/10 排第 1)。走 HTTP 契约,离线 hash 嵌入,零 token。
rerank 用一个确定性的「偏爱长文」假 reranker(词面覆盖 + 长度加分):生产实测真 reranker
就是这样把长原话顶到前面的;不装它,离线 hash 嵌入下这个缺陷复现不出来。

跑法: AIDUMEI_MEMORY_KERNEL=native python -m benchmarks.rank_raw_vs_fact [--min 10] [--min-first 8]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys

PAD = ("。顺便把前因后果都说一下：一开始以为是网络抖动，查了日志又怀疑是配置被覆盖，"
       "来回试了好几轮，中间还重启过两次服务、对过时区、清过缓存，最后才定位到真正的原因，"
       "整个过程记录在下面，方便以后回头看，也给后来的同学做个参考，免得再踩同一个坑")

TOPICS = [
    ("发卡脚本标题拼音泄漏是怎么修的",
     "发卡脚本标题出现拼音泄漏，已修复脚本的标题生成逻辑，撤回旧卡片并重新生成水墨风格正面图。",
     ["收到！我先看了一下发卡脚本，标题那一栏确实把拼音带出来了，卡片标题拼音泄漏这个问题我马上排查",
      "好的我把卡片撤回了，发卡脚本里标题拼音的那段我再跑一遍看看还会不会泄漏，水墨图也在重新生成"]),
    ("gpt-oss 在 32 token 下交白卷怎么办",
     "gpt-oss-120b 在 max_tokens=32 时思考占满预算导致正文空白；决定加入组合轮询并开启空回复自动切换。",
     ["我又测了一轮 gpt-oss，32 token 下面还是会交白卷，思考链太长了，正文一个字都没出来",
      "关于 gpt-oss 32 token 交白卷的问题，我看了判分链，max_tokens 是写死的常量，客户端放宽不了"]),
    ("手机连 Tailscale 报 DNS Unavailable 的原因",
     "手机连 Tailscale 报 DNS Unavailable 的根因是出口节点缺少 eth0 的 NAT 伪装规则，已补上并持久化。",
     ["你手机那边开了 Tailscale 出口节点以后报 DNS Unavailable 对吧，我登上去看看路由和 DNS 转发",
      "DNS Unavailable 这个报错我复现了，Tailscale 的 dns-forward-failing，我继续往下查网卡和防火墙"]),
    ("AI 生图总是出背影的原因",
     "AI 生图常出背影的根因是构图、姿态等提示词池里含有背向拍摄的指令，已删除这些条目。",
     ["生图又出背影了吗，我把最近十张都翻了一遍，确实有好几张是背影，我去看提示词怎么拼的",
      "背影这个事我查到一半，生图的提示词是从几个池子里随机抽的，AI 抽到哪条就画哪条，我再细看"]),
    ("早晚定时任务几点执行，任务ID是多少",
     "早晚两班定时维护任务的任务ID为 0192fe1f，调度为每天北京时间 06:00 和 18:00 各执行一次。",
     ["定时任务我改好了，你看一下执行时间对不对，任务ID我等下发你，早上晚上各跑一班这样可以吗",
      "这个定时任务之前是一天三班，现在改成两班了，几点执行我写在配置里了，任务ID没变"]),
    ("10router 为什么调用不了生图模型",
     "10router 调用不了生图模型的根因：其 /v1/images/generations 路由在源码里对生图通道做了硬编码限制。",
     ["我试了用 10router 调生图模型，一直报错，聊天接口那边说这个模型只能走 images 接口",
      "10router 的容器我进去看了，生图模型在清单里是有的，但是调用就是不通，我再翻翻源码"]),
    ("上下文窗口调到多少了",
     "东京 10router 上那个模型的上下文窗口已从 20 万 token 调到 1M，解决了自动回落默认值的问题。",
     ["上下文窗口我刚刚去改了，之前一直被打回默认值，我怀疑是保存的时候没生效，再试一次",
      "那个模型的上下文窗口现在显示还是二十万，我看看是不是要重启容器才会读新配置"]),
    ("主考场 dry 试跑结果怎么样",
     "主考场 dry 试跑全绿通关：10 个样本耗时 28 分钟，5/5 题全部出词且检索命中证据，F1 均值 0.567。",
     ["主考场的 dry 试跑我一直盯着，现在跑到第六个样本了，速度还可以，等全部跑完我再汇报结果",
      "dry 试跑中间有一个样本超时了，我续上了，主考场其他样本都正常，结果出来第一时间告诉你"]),
    ("杭州机器上的内存占用情况",
     "杭州机器内存：常驻服务合计峰值不到 1.2G，远低于 3.4G 物理内存，4G 和 8G 对当前系统没有差别。",
     ["杭州机器我刚 top 看了一眼内存，现在挺空的，不过我再观察一阵子峰值，别是刚好赶上空闲",
      "内存占用这块我把每个进程都列了一下，杭州机器上 Claude Code 跑起来的时候会高一点"]),
    ("评测靶机需要配 Embedding 和 Reranker 吗",
     "双盲评测中被测靶机必须配置 Embedding 和 Reranker，跑分发题平台不需要单独配置。",
     ["Embedding 和 Reranker 这两个东西我先跟你解释一下是干嘛的，评测的时候靶机那边会用到",
      "你问评测要不要配 Reranker，我看了一下官方的说明，靶机和发题平台是两回事，我理一下"]),
]
NOISE = ["周末去西湖边散步，天气很好。", "晚饭吃了片儿川，味道不错。", "提醒自己明天下午三点开会。"]


def fake_rerank(query: str, documents: list, top_n: int = 10) -> list:
    """确定性假 reranker:查询二元组覆盖率 ×0.7 + 长度加分(≤0.3),模拟真 reranker 偏爱长文。"""
    q = (query or "").lower()
    grams = {q[i:i + 2] for i in range(len(q) - 1) if q[i:i + 2].strip()}
    out = []
    for i, d in enumerate(documents):
        d = (d or "").lower()
        cov = sum(1 for g in grams if g in d) / max(1, len(grams))
        out.append({"index": i, "relevance_score": round(min(1.0, 0.7 * cov + 0.3 * min(1.0, len(d) / 300)), 4)})
    return sorted(out, key=lambda r: -r["relevance_score"])[:top_n]


def run(min_pass: int = 10, min_first: int = 8) -> dict:
    from benchmarks.regression import Adapter
    ad = Adapter("")
    import ducky.mem0_runtime as rt
    rt.rerank = fake_rerank
    try:
        for n in NOISE:
            ad._post("/add", {"messages": n, "infer": False, "metadata": {"source": "cc-ingest"}})
        for i, (_q, fact, raws) in enumerate(TOPICS):
            for j, r in enumerate(raws + ["，".join(raws)]):   # 逐轮原话:ingest 钩子同形(长),3 条
                ad._post("/add", {"messages": r + PAD, "infer": False,
                                  "metadata": {"source": "cc-ingest", "_origin_session_id": f"cc-rk-{i}-{j}"}})
            ad._post("/add", {"messages": fact, "infer": False,   # 蒸馏事实:SessionEnd 蒸馏同形
                              "metadata": {"kind": "session_distill", "lane": "distill",
                                           "_origin_session_id": f"cc-rk-{i}-0"}})
        rows = []
        for q, fact, _r in TOPICS:
            top = ad.search(q, 3)[0]
            rank = next((k + 1 for k, t in enumerate(top) if t.strip() == fact), None)
            rows.append({"q": q, "fact_rank": rank})
        n_ok = sum(1 for r in rows if r["fact_rank"])
        n_first = sum(1 for r in rows if r["fact_rank"] == 1)
        return {"pass": n_ok, "first": n_first, "n": len(rows), "ok": n_ok >= min_pass and n_first >= min_first,
                "rows": rows}
    finally:
        shutil.rmtree(ad.tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--min", type=int, default=10)
    ap.add_argument("--min-first", type=int, default=8)
    a = ap.parse_args(argv)
    res = run(a.min, a.min_first)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
