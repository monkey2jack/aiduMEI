"""LoCoMo 官方口径复刻的守卫用例。

这份文件的唯一职责：证明 ``benchmarks/locomo_official.py`` 跟 LoCoMo 官方
实现**逐字等价**。任何一条挂掉，都意味着我们报出去的分数不可比。

重点覆盖三类：
① 「看起来像 bug、但必须照抄」的官方细节（逗号先删、停用词含 and、
   cat3 截断分号、cat5 不算 F1）；
② 我做的两处「不改变结果的替换」的对拍证明（numpy.mean ↔ 算术平均、
   regex.sub ↔ re.sub）；
③ 种子锁定后 cat5 选项顺序可复现。
"""

import json
import os
import random
import re

import pytest

from benchmarks import download as bdl
from benchmarks import locomo_official as lo


# ------------------------------------------------------------ 官方 prompt 原样
def test_qa_prompt_逐字等于官方():
    assert lo.QA_PROMPT == (
        "\nBased on the above context, write an answer in the form of a short "
        "phrase for the following question. Answer with exact words from the "
        "context whenever possible.\n\nQuestion: {} Short answer:\n"
    )


def test_qa_prompt_cat5_逐字等于官方():
    assert lo.QA_PROMPT_CAT_5 == (
        "\nBased on the above context, answer the following question."
        "\n\nQuestion: {} Short answer:\n"
    )


def test_官方拼写错误_wriiten_必须保留():
    # 官方 CONV_START_PROMPT 把 written 拼成了 wriiten。改对了就不是官方口径。
    assert "wriiten" in lo.CONV_START_PROMPT
    assert "written" not in lo.CONV_START_PROMPT


def test_生成参数锁死为官方值():
    assert lo.OFFICIAL_MAX_TOKENS == 32
    assert lo.OFFICIAL_TEMPERATURE == 0


# ------------------------------------------------------------ normalize_answer
def test_归一化_逗号在最前面就被删掉():
    # 官方第一步是 s.replace(',', "")，所以 "a,b" 会粘成 "ab" 而不是 "a b"。
    # 这正是多答案切分必须发生在归一化**之前**的原因。
    assert lo.normalize_answer("a,b") == "ab"


def test_归一化_停用词表比squad多一个and():
    assert lo.normalize_answer("the cat and a dog") == "cat dog"


def test_归一化_大小写与标点():
    assert lo.normalize_answer("  Hello, World!!  ") == "hello world"


def test_归一化_and对拍标准库re与regex():
    """我把官方的 regex.sub 换成了 re.sub，这里证明两者在该模式下等价。"""
    regex_mod = pytest.importorskip("regex")
    pattern = r"\b(a|an|the|and)\b"
    for text in [
        "the cat and a dog", "an apple", "android and the band",
        "AND THE", "a", "", "xxand thexx", "and,the", "a-b-the-c",
    ]:
        low = text.lower()
        assert re.sub(pattern, " ", low) == regex_mod.sub(pattern, " ", low), text


# ------------------------------------------------------------ f1_score
def test_f1_完全一致得满分():
    pytest.importorskip("nltk", reason=(
        "LoCoMo 官方 F1 依赖 nltk 的 PorterStemmer（换实现就不是官方口径）。"
        "缺它是「没装 bench 可选依赖」，不是缺陷 —— 与 regex/numpy 一个待遇。"))
    assert lo.f1_score("New York", "New York") == pytest.approx(1.0)


def test_f1_完全不沾边得零分():
    pytest.importorskip("nltk", reason=(
        "LoCoMo 官方 F1 依赖 nltk 的 PorterStemmer（换实现就不是官方口径）。"
        "缺它是「没装 bench 可选依赖」，不是缺陷 —— 与 regex/numpy 一个待遇。"))
    assert lo.f1_score("banana", "New York") == 0


def test_f1_做词干化():
    # PorterStemmer 把 running / runs 都归到 run，官方就是这么算的。
    pytest.importorskip("nltk", reason=(
        "LoCoMo 官方 F1 依赖 nltk 的 PorterStemmer（换实现就不是官方口径）。"
        "缺它是「没装 bench 可选依赖」，不是缺陷 —— 与 regex/numpy 一个待遇。"))
    assert lo.f1_score("running", "runs") == pytest.approx(1.0)


def test_f1_部分重叠():
    # pred=2 词, gold=3 词, 交集 2 → P=1.0 R=2/3 → F1=0.8
    pytest.importorskip("nltk", reason=(
        "LoCoMo 官方 F1 依赖 nltk 的 PorterStemmer（换实现就不是官方口径）。"
        "缺它是「没装 bench 可选依赖」，不是缺陷 —— 与 regex/numpy 一个待遇。"))
    assert lo.f1_score("New York", "New York City") == pytest.approx(0.8)


def test_f1_是多重集不是集合():
    # Counter 交集会计重复次数：pred 里两个 "a" 只能匹配 gold 里的一个。
    pytest.importorskip("nltk", reason=(
        "LoCoMo 官方 F1 依赖 nltk 的 PorterStemmer（换实现就不是官方口径）。"
        "缺它是「没装 bench 可选依赖」，不是缺陷 —— 与 regex/numpy 一个待遇。"))
    score_dup = lo.f1_score("cat cat", "cat")
    assert score_dup == pytest.approx(2 * (0.5 * 1.0) / (0.5 + 1.0))


def test_多答案f1_按逗号切分后逐个取最优():
    # gold 两项都被 pred 命中 → 1.0
    pytest.importorskip("nltk", reason=(
        "LoCoMo 官方 F1 依赖 nltk 的 PorterStemmer（换实现就不是官方口径）。"
        "缺它是「没装 bench 可选依赖」，不是缺陷 —— 与 regex/numpy 一个待遇。"))
    assert lo.f1("Tokyo, Paris", "Paris, Tokyo") == pytest.approx(1.0)
    # gold 两项只命中一项 → (1.0 + 0.0) / 2
    assert lo.f1("Tokyo", "Tokyo, Paris") == pytest.approx(0.5)


def test_多答案f1_对拍numpy_mean():
    """我把官方的 np.mean 换成了 sum/len，这里证明等价。"""
    pytest.importorskip("nltk", reason=(
        "LoCoMo 官方 F1 依赖 nltk 的 PorterStemmer（换实现就不是官方口径）。"
        "缺它是「没装 bench 可选依赖」，不是缺陷 —— 与 regex/numpy 一个待遇。"))
    np = pytest.importorskip("numpy")
    cases = [
        ("Tokyo, Paris", "Paris, Tokyo"),
        ("Tokyo", "Tokyo, Paris"),
        ("a, b, c", "c, a, x"),
        ("New York", "New York City, Boston"),
    ]
    for pred, gold in cases:
        predictions = [p.strip() for p in pred.split(",")]
        ground_truths = [g.strip() for g in gold.split(",")]
        official = float(np.mean(
            [max([lo.f1_score(p, gt) for p in predictions]) for gt in ground_truths]
        ))
        assert lo.f1(pred, gold) == pytest.approx(official), (pred, gold)


# ------------------------------------------------------------ 题面构造
def test_cat2追加官方日期后缀():
    q, key = lo.build_question({"category": 2, "question": "When did it happen?"},
                               random.Random(0))
    assert q == ("When did it happen? Use DATE of CONVERSATION to answer "
                 "with an approximate date.")
    assert key is None


def test_cat1和cat4题面原样不动():
    for cat in (1, 3, 4):
        q, key = lo.build_question({"category": cat, "question": "Who?"},
                                   random.Random(0))
        assert q == "Who?"
        assert key is None


def test_cat5是随机二选一而不是拒答提示():
    q, key = lo.build_question(
        {"category": 5, "question": "What car?", "answer": "a red Volvo"},
        random.Random(0))
    assert "Select the correct answer: (a) " in q
    assert "Not mentioned in the conversation" in q
    assert "a red Volvo" in q
    # 两个选项一个是金标、一个是拒答项，顺序随机
    assert set(key.values()) == {"Not mentioned in the conversation", "a red Volvo"}


def test_cat5选项顺序在锁种子后可复现():
    qa = {"category": 5, "question": "What car?", "answer": "a red Volvo"}
    first = [lo.build_question(qa, random.Random(1234))[0] for _ in range(3)]
    assert len(set(first)) == 1, "同一种子必须给出同一顺序"
    # 不同种子应当能产生两种顺序（否则说明分支没被真正随机走到）
    seen = {lo.build_question(qa, random.Random(s))[0] for s in range(30)}
    assert len(seen) == 2, "官方是二选一随机，应恰有两种排列"


def test_cat5还原选项_官方松判据():
    key = {"a": "Not mentioned in the conversation", "b": "a red Volvo"}
    assert lo.get_cat_5_answer("a", key) == key["a"]
    assert lo.get_cat_5_answer("b", key) == key["b"]
    assert lo.get_cat_5_answer("(a)", key) == key["a"]
    assert lo.get_cat_5_answer("(b)", key) == key["b"]
    # 长度既不是 1 也不是 3 时，官方原样返回（小写化后）
    assert lo.get_cat_5_answer("A Red Volvo", key) == "a red volvo"


# ------------------------------------------------------------ 上下文与 query
def test_上下文用单换行拼接且带时间戳():
    items = [
        {"memory": "去了海边", "timestamp": "1:56 pm on 8 May, 2023"},
        {"memory": "买了相机", "timestamp": "2:10 pm on 9 May, 2023"},
    ]
    assert lo.build_context(items) == (
        "1:56 pm on 8 May, 2023: 去了海边\n2:10 pm on 9 May, 2023: 买了相机"
    )


def test_上下文没有时间戳时不硬造():
    assert lo.build_context([{"memory": "只有正文"}]) == "只有正文"


def test_上下文跳过空正文():
    assert lo.build_context([{"memory": ""}, {"memory": "有货"}]) == "有货"


def test_query拼法按题型选模板():
    q1 = lo.build_query("CTX", "问题?", category=1)
    assert q1.startswith("CTX\n\n")
    assert "write an answer in the form of a short phrase" in q1
    q5 = lo.build_query("CTX", "问题?", category=5)
    assert q5.startswith("CTX\n\n")
    assert "write an answer in the form of a short phrase" not in q5


# ------------------------------------------------------------ 评分路由
def test_cat3金标截断到第一个分号():
    # 金标 "Sunday; the weekend" 只取 "Sunday"，所以答 "Sunday" 是满分。
    pytest.importorskip("nltk", reason=(
        "LoCoMo 官方 F1 依赖 nltk 的 PorterStemmer（换实现就不是官方口径）。"
        "缺它是「没装 bench 可选依赖」，不是缺陷 —— 与 regex/numpy 一个待遇。"))
    assert lo.score_one(3, "Sunday", "Sunday; the weekend") == pytest.approx(1.0)


def test_cat3不截断的话就不是满分_负向对照():
    # 同一份预测按「不截断」口径只有 2/3（金标多出 weekend 一词），
    # 明显低于上一条的满分，证明 cat3 确实走了截断分支。
    pytest.importorskip("nltk", reason=(
        "LoCoMo 官方 F1 依赖 nltk 的 PorterStemmer（换实现就不是官方口径）。"
        "缺它是「没装 bench 可选依赖」，不是缺陷 —— 与 regex/numpy 一个待遇。"))
    assert lo.f1_score("Sunday", "Sunday; the weekend") == pytest.approx(2 / 3)


def test_cat1走多答案f1_而不是单答案f1():
    pytest.importorskip("nltk", reason=(
        "LoCoMo 官方 F1 依赖 nltk 的 PorterStemmer（换实现就不是官方口径）。"
        "缺它是「没装 bench 可选依赖」，不是缺陷 —— 与 regex/numpy 一个待遇。"))
    pred, gold = "Tokyo", "Tokyo, Paris"
    assert lo.score_one(1, pred, gold) == pytest.approx(lo.f1(pred, gold))
    # 负向对照：单答案口径给的是另一个数，说明路由确实分叉了
    assert lo.f1_score(pred, gold) != pytest.approx(lo.f1(pred, gold))


def test_cat5不算f1_只判是否拒答():
    assert lo.score_one(5, "No information available", "whatever") == 1.0
    assert lo.score_one(5, "not mentioned in the conversation", "whatever") == 1.0
    assert lo.score_one(5, "a red Volvo", "a red Volvo") == 0.0, \
        "cat5 答对了金标反而是 0 分——这是官方口径，不是 bug"


def test_未知题型必须抛错而不是静默给零分():
    with pytest.raises(ValueError):
        lo.score_one(9, "x", "y")


# ------------------------------------------------------------ 汇总
def test_汇总给出总均值与分题型均值():
    recs = [
        {"category": 1, "locomo_score": 1.0},
        {"category": 1, "locomo_score": 0.0},
        {"category": 5, "locomo_score": 1.0},
    ]
    out = lo.score_all(recs)
    assert out["total_questions"] == 3
    assert out["overall_mean"] == pytest.approx(2 / 3)
    assert out["by_category"]["1"] == {"n": 2, "mean": pytest.approx(0.5)}
    assert out["by_category"]["5"] == {"n": 1, "mean": pytest.approx(1.0)}


def test_空记录不炸并给出None():
    out = lo.score_all([])
    assert out["total_questions"] == 0
    assert out["overall_mean"] is None


def test_上下文时间戳认适配器实际写的metadata_recorded_at():
    """适配器把时间戳写进 ``metadata.recorded_at``（见 adapter.add_turn）。

    如果 build_context 只认顶层 ``timestamp``，抽取路径召回的条目就会
    整段丢掉日期——而 category 2 全靠日期作答。
    """
    items = [{"memory": "去了巴黎", "metadata": {"recorded_at": "2023年5月1日"}}]
    assert lo.build_context(items) == "2023年5月1日: 去了巴黎"


def test_上下文没时间戳就只放正文_不硬造_负向对照():
    """verbatim 召回路径不带 metadata；此时不许编一个日期出来。"""
    assert lo.build_context([{"memory": "去了巴黎"}]) == "去了巴黎"
    assert lo.build_context([{"memory": "x", "metadata": None}]) == "x"


def test_顶层timestamp优先于metadata():
    items = [{"memory": "x", "timestamp": "顶层",
              "metadata": {"recorded_at": "元数据"}}]
    assert lo.build_context(items) == "顶层: x"


# ── cat5 诱答字段：公开数据集与官方脚本不一致 ────────────────────────

def test_cat5取诱答优先adversarial_answer():
    """真实 locomo10.json 里 444/446 道 cat5 只有 adversarial_answer。"""
    qa = {"category": 5, "question": "What car?",
          "adversarial_answer": "a red Volvo"}
    text, key = lo.build_question(qa, random.Random(0))
    assert "a red Volvo" in text
    assert set(key.values()) == {"a red Volvo", lo.CAT5_ABSTAIN_OPTION}


def test_cat5两个键都有时诱答仍是adversarial_answer():
    """数据集里那 2 道特例：adversarial='Yes'、answer='No'，诱答是 Yes。"""
    qa = {"category": 5, "question": "Did she make it?",
          "adversarial_answer": "Yes", "answer": "No"}
    text, key = lo.build_question(qa, random.Random(0))
    assert "Yes" in text and "No" not in set(key.values())
    assert set(key.values()) == {"Yes", lo.CAT5_ABSTAIN_OPTION}


def test_cat5两个键都缺就报错而不是编一个选项():
    """负向对照：缺诱答就停，不许静默造选项——那等于自己改考卷。"""
    with pytest.raises(ValueError, match="category 5 缺少诱答文本"):
        lo.build_question({"category": 5, "question": "?"}, random.Random(0))
    with pytest.raises(ValueError):
        lo.build_question({"category": 5, "question": "?",
                           "adversarial_answer": "   "}, random.Random(0))


def test_全量数据集每道cat5都能构出选项():
    """把真实数据集扫一遍：不能有任何一道 cat5 构不出题面。

    这条是「跑到一半炸」的对照——上一版 build_question 会在这里挂掉 444 次。
    数据集不在仓库里，缺文件时跳过（本机与生产都有）。路径走产品自己的
    ``benchmarks.download.data_dir()``——它认 ``AIDUMEI_BENCH_DATA_DIR``，
    所以沙箱跑测能改道，且跑测与产品共用同一份路径判据，不会各写一套。
    """
    path = os.path.join(bdl.data_dir(), "locomo10.json")
    if not os.path.exists(path):
        pytest.skip("本机无 locomo10.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    n5 = 0
    for sample in data:
        rng = random.Random(0)
        for qa in sample["qa"]:
            text, key = lo.build_question(qa, rng)
            assert text
            if int(qa["category"]) == 5:
                n5 += 1
                assert lo.CAT5_ABSTAIN_OPTION in set(key.values())
            else:
                assert key is None
    assert n5 == 446, n5
