"""LoCoMo 官方评分口径的逐字复刻。

**这份文件不发明任何评分规则。** 它把 LoCoMo 官方仓库
``task_eval/evaluation.py`` 与 ``task_eval/gpt_utils.py`` 里跟打分直接
相关的那几个函数，一比一搬过来，只做三件不改变结果的事：

1. 把 ``numpy.mean`` 换成等价的算术平均（避免 benchmarks 为了一个求均值
   而硬依赖 numpy）——``tests`` 里有与 numpy 的对拍用例；
2. 把 ``regex.sub`` 换成标准库 ``re.sub``（该模式是纯 ASCII 字面量，两者
   行为一致）——同样有对拍用例；
3. 补中文注释，说明每一处「看起来像 bug 但必须照抄」的地方。

**照抄清单（改一个字，分数就不可比了）：**

- ``normalize_answer`` 先 ``replace(',', "")`` 再去冠词，且停用词表是
  ``a|an|the|and``——比通行的 SQuAD 口径多一个 ``and``。
- ``f1_score`` 的分词做 Porter 词干化后用 ``Counter`` 求多重集交集。
- ``f1``（多答案版）先按英文逗号切分，再对每个标准答案取最优匹配后平均。
- category 3 的标准答案在评分前**截断到第一个分号**。
- category 5 **不算 F1**，只做二值判定：模型输出里出现
  ``no information available`` 或 ``not mentioned`` 记 1 分，否则 0 分。
- category 2 的**问题**要追加 ``Use DATE of CONVERSATION ...`` 后缀。
- category 5 的**问题**要改写成随机顺序的二选一，其中一项固定是
  ``Not mentioned in the conversation``。官方用了未设种子的
  ``random.random()``；我们必须锁种子，否则同一份数据两次跑分不可复现
  （见 ``PROTOCOL.md`` §4）。
- ``CONV_START_PROMPT`` 里的 ``wriiten`` 是官方的拼写错误，**原样保留**。
- 官方在 RAG 模式下**不加** ``CONV_START_PROMPT``；我们是记忆检索场景，
  等同 RAG 模式，所以也不加。

上游：github.com/snap-research/locomo，commit
``3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376``（见 ``PROTOCOL.md`` §1）。
数据集许可 CC BY-NC 4.0，**仅限非商业评测**。
"""

from __future__ import annotations

import random
import re
import string
from collections import Counter

__all__ = [
    "QA_PROMPT", "QA_PROMPT_CAT_5", "CONV_START_PROMPT",
    "normalize_answer", "f1_score", "f1", "exact_match_score",
    "build_question", "build_context", "build_query",
    "get_cat_5_answer", "score_one", "score_all",
]

# ---------------------------------------------------------------- 官方 prompt
# 逐字照抄 task_eval/gpt_utils.py：首尾换行、两个空行、结尾冒号都算口径。
QA_PROMPT = """
Based on the above context, write an answer in the form of a short phrase for the following question. Answer with exact words from the context whenever possible.

Question: {} Short answer:
"""

QA_PROMPT_CAT_5 = """
Based on the above context, answer the following question.

Question: {} Short answer:
"""

# 官方原文的 "wriiten" 拼写错误保留。RAG 模式下官方并不使用这一段，
# 此处留档只为让「我们没用它」这件事可被查证。
CONV_START_PROMPT = (
    "Below is a conversation between two people: {} and {}. The conversation "
    "takes place over multiple days and the date of each conversation is "
    "wriiten at the beginning of the conversation.\n\n"
)

# 官方 run_chatgpt(..., num_tokens_request=32, temperature=0)
OFFICIAL_MAX_TOKENS = 32
OFFICIAL_TEMPERATURE = 0

# category 2 的问题后缀，官方原文一字不差（注意句末的句号）。
CAT2_SUFFIX = " Use DATE of CONVERSATION to answer with an approximate date."
# category 5 的选择题模板，官方原文一字不差（注意末尾那个空格）。
CAT5_TEMPLATE = " Select the correct answer: (a) {} (b) {}. "
CAT5_ABSTAIN_OPTION = "Not mentioned in the conversation"


# ---------------------------------------------------------------- 词干化
def _stemmer():
    """惰性加载 PorterStemmer：benchmarks 是可选模块，产品主路径不该被它拖住。"""
    try:
        from nltk.stem import PorterStemmer
    except ImportError as exc:  # pragma: no cover - 环境缺失时的显式指路
        raise RuntimeError(
            "LoCoMo 官方 F1 依赖 nltk 的 PorterStemmer。装它：pip install nltk。"
            "不要用别的词干化实现顶替——换一个 stemmer，分数就不是官方口径了。"
        ) from exc
    return PorterStemmer()


_PS = None


def _stem(word: str) -> str:
    global _PS
    if _PS is None:
        _PS = _stemmer()
    return _PS.stem(word)


# ---------------------------------------------------------------- 归一化与 F1
def normalize_answer(s: str) -> str:
    """官方 normalize_answer。

    注意三处与通行 SQuAD 口径的差异，都必须保留：
    ① 逗号在最前面就被删掉（所以多答案切分必须在调用本函数**之前**做）；
    ② 停用词表多一个 ``and``；
    ③ 去标点用 ``string.punctuation`` 全集。
    """
    s = s.replace(",", "")

    def remove_articles(text: str) -> str:
        # 官方用的是 regex.sub；此模式为纯 ASCII 字面量，标准库 re 完全等价。
        return re.sub(r"\b(a|an|the|and)\b", " ", text)

    def white_space_fix(text: str) -> str:
        return " ".join(text.split())

    def remove_punc(text: str) -> str:
        exclude = set(string.punctuation)
        return "".join(ch for ch in text if ch not in exclude)

    def lower(text: str) -> str:
        return text.lower()

    return white_space_fix(remove_articles(remove_punc(lower(s))))


def exact_match_score(prediction: str, ground_truth: str) -> bool:
    """官方 exact_match_score。官方评测主路径并不使用它，留档以备核对。"""
    prediction = normalize_answer(prediction)
    ground_truth = normalize_answer(ground_truth)
    return set(prediction.split()) == set(ground_truth.split())


def f1_score(prediction: str, ground_truth: str) -> float:
    """官方 f1_score：Porter 词干化后的多重集 F1。"""
    prediction_tokens = [_stem(w) for w in normalize_answer(prediction).split()]
    ground_truth_tokens = [_stem(w) for w in normalize_answer(ground_truth).split()]
    common = Counter(prediction_tokens) & Counter(ground_truth_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        # 官方在这里 return 0（int），不是 0.0；数值等价，照抄语义。
        return 0
    precision = 1.0 * num_same / len(prediction_tokens)
    recall = 1.0 * num_same / len(ground_truth_tokens)
    return (2 * precision * recall) / (precision + recall)


def f1(prediction: str, ground_truth: str) -> float:
    """官方 f1（多答案版，category 1 专用）。

    先按英文逗号切分成子答案，对每个标准子答案取「所有预测子答案里的最优
    F1」，再把这些最优值平均。官方用 ``np.mean``；对 float 列表而言
    ``sum/len`` 与之等价（tests 里有对拍）。
    """
    predictions = [p.strip() for p in prediction.split(",")]
    ground_truths = [g.strip() for g in ground_truth.split(",")]
    per_gt = [max(f1_score(p, gt) for p in predictions) for gt in ground_truths]
    return sum(per_gt) / len(per_gt)


# ---------------------------------------------------------------- 提问侧
def _cat5_distractor(qa: dict) -> str:
    """取 category 5 的「诱答」选项文本。

    官方脚本写的是 ``qa['answer']``，但**公开发布的 locomo10.json 里
    1986 题中有 444 道 category 5 根本没有 ``answer`` 键**（只有
    ``adversarial_answer``），照抄官方会直接 KeyError 打死 22.5% 的考题。
    另有 2 道两个键都有，且 ``adversarial_answer='Yes'`` /
    ``answer='No'``——诱答是前者。所以优先级是 adversarial_answer 在前。

    两个键都没有就抛错，不静默造一个选项：cat 5 的两个选项决定题面，
    编一个出来等于自己改考卷。
    """
    for key in ("adversarial_answer", "answer"):
        if key in qa and str(qa[key]).strip():
            return str(qa[key])
    raise ValueError(
        "category 5 缺少诱答文本（adversarial_answer / answer 都没有），"
        "无法构造官方的 (a)/(b) 选项")


def build_question(qa: dict, rng: random.Random) -> tuple[str, dict | None]:
    """按官方口径把原始题面改写成实际发给模型的问题。

    返回 ``(question, cat5_answer_key)``；只有 category 5 才有第二项，用于
    事后把模型答的 (a)/(b) 还原成选项文本。

    ``rng`` 必须是外部传入的、种子已锁定的 ``random.Random``：官方用了全局
    ``random.random()`` 且未设种子，导致同一份数据两次跑分的 category 5 选项
    顺序不同。顺序会影响模型作答，所以不锁种子就不可复现。
    """
    category = int(qa["category"])
    question = str(qa["question"])
    if category == 2:
        return question + CAT2_SUFFIX, None
    if category == 5:
        gold = _cat5_distractor(qa)
        if rng.random() < 0.5:
            text = question + CAT5_TEMPLATE.format(CAT5_ABSTAIN_OPTION, gold)
            key = {"a": CAT5_ABSTAIN_OPTION, "b": gold}
        else:
            text = question + CAT5_TEMPLATE.format(gold, CAT5_ABSTAIN_OPTION)
            key = {"b": CAT5_ABSTAIN_OPTION, "a": gold}
        return text, key
    return question, None


def build_context(items: list[dict]) -> str:
    """把检索命中的记忆拼成官方 RAG 口径的上下文。

    官方 ``get_rag_context`` 在 dialog / observation 模式下拼成
    ``date_time + ': ' + context``，用单个换行连接。我们的检索结果就是对话
    片段，对应 dialog 模式。没有时间戳的条目不硬造，直接只放正文。

    时间戳有三个可能的落点：适配器实际写进的是 ``metadata.recorded_at``
    （见 ``adapter.add_turn``），另两个是兼容形状。verbatim 召回路径回来的
    item 没有 metadata，此时就只有正文——这正是「不硬造」要处理的情形。
    """
    lines = []
    for item in items:
        if not isinstance(item, dict):
            lines.append(str(item))
            continue
        text = item.get("memory") or item.get("text") or item.get("content") or ""
        text = str(text).strip()
        if not text:
            continue
        meta = item.get("metadata")
        stamp = str(
            item.get("timestamp")
            or item.get("date_time")
            or (meta.get("recorded_at") if isinstance(meta, dict) else "")
            or ""
        ).strip()
        lines.append(f"{stamp}: {text}" if stamp else text)
    return "\n".join(lines)


def build_query(context: str, question: str, category: int) -> str:
    """官方 batch_size==1 的拼法：上下文 + 两个换行 + 对应的 QA_PROMPT。"""
    template = QA_PROMPT_CAT_5 if int(category) == 5 else QA_PROMPT
    return context + "\n\n" + template.format(question)


def get_cat_5_answer(model_prediction: str, answer_key: dict) -> str:
    """官方 get_cat_5_answer：把模型答的 a / (a) 还原成选项文本。

    官方的判据就是这么松：长度为 1 时看有没有 'a'，长度为 3 时看有没有
    '(a)'，其余一律原样返回。照抄。
    """
    model_prediction = model_prediction.strip().lower()
    if len(model_prediction) == 1:
        return answer_key["a"] if "a" in model_prediction else answer_key["b"]
    if len(model_prediction) == 3:
        return answer_key["a"] if "(a)" in model_prediction else answer_key["b"]
    return model_prediction


# ---------------------------------------------------------------- 评分侧
def score_one(category: int, prediction: str, gold: str) -> float:
    """官方 eval_question_answering 的单题路由。

    category 5 **不算 F1**，只看模型有没有正确拒答。官方对未知 category
    直接 raise，这里同样不吞——出现新题型就该停下来看清楚。
    """
    category = int(category)
    gold = str(gold)
    if category == 3:
        # 官方：category 3 的标准答案截断到第一个分号。
        gold = gold.split(";")[0].strip()
    if category in (2, 3, 4):
        return float(f1_score(prediction, gold))
    if category == 1:
        return float(f1(prediction, gold))
    if category == 5:
        low = prediction.lower()
        return 1.0 if ("no information available" in low or "not mentioned" in low) else 0.0
    raise ValueError(f"LoCoMo 未知题型 category={category}；官方口径只认 1-5")


def score_all(records: list[dict]) -> dict:
    """按官方口径汇总。

    官方 ``eval_question_answering`` 返回一个拉平的 ``all_ems`` 列表，报告里
    通常按 category 分别取均值。这里两样都给：总均值 + 分题型均值 + 题数，
    并且**不四舍五入**——要对外引用的数字必须能对上原始记录。
    """
    per_cat: dict[int, list[float]] = {}
    for rec in records:
        cat = int(rec["category"])
        per_cat.setdefault(cat, []).append(float(rec["locomo_score"]))
    out = {
        "total_questions": len(records),
        "overall_mean": (sum(float(r["locomo_score"]) for r in records) / len(records))
        if records else None,
        "by_category": {
            str(cat): {"n": len(vals), "mean": sum(vals) / len(vals)}
            for cat, vals in sorted(per_cat.items())
        },
        "note": (
            "官方口径：cat 1 多答案 F1；cat 2/3/4 单答案 F1（cat 3 金标截断到"
            "首个分号）；cat 5 不算 F1，只判是否正确拒答。均值未四舍五入。"
        ),
    }
    return out
