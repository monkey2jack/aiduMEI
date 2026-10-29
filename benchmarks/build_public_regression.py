"""公开版回归集生成器(完全虚构语料,确定性,种子固定)。

为什么有它:生产回归集 v3 取自真实使用记录(已脱敏),不进公开仓。
公开仓用本脚本生成的同 schema、同规模、同六类配比的虚构集代替,
让任何人都能复现判分器与内核行为;两套集合的分数不可互相比较。

用法:python -m benchmarks.build_public_regression --out FILE
     发布公开仓时(f0.7 P2-5):在导出树里 --out benchmarks/regression_v3.json --replace-real,
     再用虚构集重录 regression_v3_baseline*.json。私有仓里不许覆盖真实集,所以不带 --replace-real 时拒写该路径。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

SEED = 20261028
OUT = Path(__file__).parent / "regression_v3.json"

SERVICES = ["青岚网关", "白鹭调度器", "松果缓存", "海盐日志站", "铜铃告警", "竹简文档库", "灯塔监控",
            "飞鱼消息队列", "石榴对象存储", "云杉构建机", "琥珀向量库", "麦穗任务板", "银杏备份器",
            "山茶反向代理", "雪豹搜索", "陶罐配置中心", "纸鸢推送", "橡果计费", "蓝鲸数据仓", "萤火边缘节点",
            "柚子工单", "雀羽认证", "花椒限流器", "芦苇爬虫", "墨鱼翻译服务", "珊瑚画图服务", "砚台笔记",
            "风铃语音网关", "樱桃前端", "核桃定时器", "槐花邮件中继", "鲸歌播客站", "梧桐知识库", "荔枝客服机器人",
            "薄荷沙箱", "枫叶统计", "豆荚容器平台", "月桂审计日志", "海棠短链", "苔藓归档"]
HOSTS = ["node-a", "node-b", "node-c", "node-d", "edge-1", "edge-2", "lab-1", "lab-2"]
REGIONS = ["华东一区", "华北二区", "东京区", "法兰克福区", "新加坡区", "硅谷区"]
LANGS = ["Go", "Rust", "Python", "TypeScript", "Kotlin", "Elixir"]
MODELS = ["星图-7B", "长河-14B", "微光-3B", "远帆-32B", "鹊桥-8B"]
OWNERS = ["运维组", "平台组", "数据组", "前端组", "安全组", "值班同学"]
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

ATTRS = [  # (属性名, 取值函数, 问法模板列表)
    ("监听端口", lambda r: str(r.randint(2000, 9899)), ["{s} 用的是哪个端口", "{s} 监听在几号端口"]),
    ("部署机器", lambda r: r.choice(HOSTS), ["{s} 部署在哪台机器上", "{s} 跑在哪个节点"]),
    ("所在机房", lambda r: r.choice(REGIONS), ["{s} 放在哪个区域", "{s} 部署在哪个机房"]),
    ("实现语言", lambda r: r.choice(LANGS), ["{s} 是用什么语言写的", "{s} 的开发语言是什么"]),
    ("负责团队", lambda r: r.choice(OWNERS), ["{s} 归谁管", "{s} 由哪个团队负责"]),
    ("默认模型", lambda r: r.choice(MODELS), ["{s} 默认调用哪个模型", "{s} 用的是什么模型"]),
    ("内存上限", lambda r: f"{r.choice([512, 768, 1024, 1536, 2048, 4096])}MB", ["{s} 的内存上限是多少", "{s} 最多能用多少内存"]),
    ("每日备份时间", lambda r: f"{r.randint(0, 5):02d}:{r.choice(['00', '15', '30', '45'])}", ["{s} 每天几点备份", "{s} 的备份时间是几点"]),
    ("数据保留天数", lambda r: f"{r.choice([7, 14, 30, 60, 90, 180])} 天", ["{s} 的数据保留多久", "{s} 数据留几天"]),
    ("周维护窗口", lambda r: f"{r.choice(WEEKDAYS)}凌晨", ["{s} 的维护窗口在什么时候", "{s} 哪天做维护"]),
]

FILLERS = ["这条是上周排查时顺手记下的。", "以后改动前先看一眼这里。", "值班手册里也同步写了。",
           "别和测试环境的配置弄混。", "这是目前线上的真实设置。", ""]
CHAT_OPENERS = ["我刚看了一下，", "顺便说一句，", "跟你同步个情况，", "刚才排查的时候发现，", "提醒一下，"]
CHAT_TAILS = ["你先记着，回头我们再细看。", "不急，有空再处理就行。", "我晚点再确认一遍数字。",
              "这块之前好像出过一次小问题，我们后面留意下。", "先这样，其他的明天再说。"]


def _fact_text(r, s, attr, val):
    return f"{s}的{attr}是{val}。{r.choice(FILLERS)}".strip()


def _chat_text(r, s, attr, val):
    """长口语原话:同一事实埋在闲聊里,用来检验'提炼事实 vs 长原话'的排序。"""
    return (f"{r.choice(CHAT_OPENERS)}{s}那边我登上去转了一圈，日志看着还算干净，"
            f"顺手把{attr}也核对了下，现在是{val}，跟文档对得上。另外磁盘和连接数都正常，"
            f"没有报警。{r.choice(CHAT_TAILS)}")


def build() -> dict:
    r = random.Random(SEED)
    facts = []                                   # (service, attr, val, asks)
    for s in SERVICES:
        for attr, gen, asks in r.sample(ATTRS, 8):
            facts.append((s, attr, gen(r), asks))
    r.shuffle(facts)
    pool = iter(facts)
    corpus: list[str] = []
    cases: list[dict] = []

    def take():
        return next(pool)

    for i in range(51):                                       # fact_recall
        s, attr, val, asks = take()
        t = _fact_text(r, s, attr, val)
        corpus.append(t)
        if i % 3 == 0:
            corpus.append(_chat_text(r, s, attr, val))       # 三分之一带一条长原话干扰
        cases.append({"id": f"fr-{i + 1:03d}", "category": "fact_recall", "query": r.choice(asks).format(s=s),
                      "src": "synthetic", "expect": [t]})

    events = ["完成版本升级", "完成数据迁移", "通过安全复查", "完成灾备演练", "切换到新机房", "完成证书轮换"]
    used = set()
    for i in range(25):                                       # temporal
        while True:
            s, ev = r.choice(SERVICES), r.choice(events)
            if (s, ev) not in used:
                used.add((s, ev))
                break
        date = f"2026 年 {r.randint(1, 9)} 月 {r.randint(1, 28)} 日"
        t = f"时间锚点：{date}，{s}{ev}，由{r.choice(OWNERS)}确认。"
        corpus.append(t)
        cases.append({"id": f"tm-{i + 1:03d}", "category": "temporal", "query": f"{s}是哪天{ev}的",
                      "src": "synthetic", "expect": [t]})

    abstain_q = ["我的护照号码是多少", "家里那只猫叫什么名字", "上次去冰岛旅行住的是哪家酒店", "我对花生过敏吗",
                 "上个月家里电费交了多少钱", "我的车牌号是多少", "公司 VPN 服务器的地址是什么", "Kubernetes 集群用的是哪个版本",
                 "Redis 的最大内存配置成了多少", "上周五周会的纪要写了什么", "Jenkins 流水线的触发条件是什么",
                 "MongoDB 副本集有几个节点", "我常去的健身房叫什么", "牙医预约在哪天", "女儿的钢琴课是周几",
                 "Oracle 数据库的 SID 是什么", "我的社保卡号是多少", "年终奖发了多少", "家里宽带是哪家运营商",
                 "最喜欢的足球队是哪支", "Kafka 的分区数设置了多少", "上次量的血压是多少", "房贷每月还多少",
                 "Elasticsearch 集群的分片策略是什么", "我老家的邮编是多少"]
    for i, q in enumerate(abstain_q):                         # abstain
        cases.append({"id": f"ab-{i + 1:03d}", "category": "abstain", "query": q, "src": "authored"})

    for i in range(25):                                       # update
        s, attr, val, asks = take()
        old = _fact_text(r, s, attr, val)
        new_val = val
        gen = dict((a, g) for a, g, _ in ATTRS)[attr]
        while new_val == val:
            new_val = gen(r)
        new = f"{s}的{attr}已改为{new_val}（旧值{val}作废）。"
        corpus.append(old)
        cases.append({"id": f"up-{i + 1:03d}", "category": "update", "query": r.choice(asks).format(s=s),
                      "src": "synthetic", "steps": [{"op": "update", "old": old, "text": new}],
                      "expect": [new], "forbid": [old]})

    for i in range(25):                                       # delete
        s, attr, val, asks = take()
        t = _fact_text(r, s, attr, val)
        corpus.append(t)
        cases.append({"id": f"de-{i + 1:03d}", "category": "delete", "query": r.choice(asks).format(s=s),
                      "src": "synthetic", "steps": [{"op": "check", "expect": [t]}, {"op": "delete", "text": t}],
                      "forbid": [t]})

    for i in range(20):                                       # cc_replay:上一会话说的话,下一会话能被注入
        s, attr, val, asks = take()
        said = f"助手，{s}的{attr}我刚改成{val}了，你记一下。"
        cases.append({"id": f"cc-{i + 1:03d}", "category": "cc_replay", "query": r.choice(asks).format(s=s),
                      "src": "synthetic", "steps": [{"op": "add", "text": said, "session": f"cc{i + 1:02d}-s1"}],
                      "session": f"cc{i + 1:02d}-next", "expect": [said]})

    for s, attr, val, _ in pool:                              # 其余事实作干扰项,补到 415 条
        if len(corpus) >= 415:
            break
        corpus.append(_fact_text(r, s, attr, val))
        if len(corpus) < 415 and r.random() < 0.7:
            corpus.append(_chat_text(r, s, attr, val))
    r.shuffle(corpus)
    return {
        "schema": "regression.v3", "version": "v3.0-public-synthetic", "generated_at": "2026-10-28",
        "notes": ("PUBLIC SYNTHETIC SUBSTITUTE. 全部语料与问题为虚构(由 benchmarks/build_public_regression.py "
                  f"以种子 {SEED} 生成),不含任何真实用户数据。生产用的真实回归集 v3 不公开;"
                  "公开集与私有集结构/规模/类别配比相同,但分数不可互比。"),
        "per_category": {c: sum(1 for x in cases if x["category"] == c)
                         for c in ("fact_recall", "temporal", "update", "delete", "abstain", "cc_replay")},
        "corpus": [{"key": f"syn-{i}", "text": t} for i, t in enumerate(corpus)],
        "cases": cases,
    }


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="公开版虚构回归集生成器")
    ap.add_argument("--out", required=True)
    ap.add_argument("--replace-real", action="store_true", help="允许覆盖 benchmarks/regression_v3.json(只在公开导出树里用)")
    a = ap.parse_args(argv)
    out = Path(a.out).resolve()
    if out == OUT.resolve() and not a.replace_real:
        raise SystemExit("拒绝覆盖真实回归集 regression_v3.json;公开导出时加 --replace-real")
    data = build()
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(len(data["corpus"]), "corpus,", len(data["cases"]), "cases ->", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
