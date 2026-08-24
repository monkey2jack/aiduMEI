"""答题模型客户端：正式跑分里「生成答案」的那一侧。

跑分管线原本只有**检索**一半（``run.py`` 出的是召回诊断），没有答案，
也就没有分数。这个模块补上另一半：把检索结果按官方口径拼成 prompt，
调答题模型，拿回一句短答案，交给 ``locomo_official.score_one`` 打分。

**四条硬约束，写在最前面：**

1. **生成参数锁死官方值**（``temperature=0``、``max_tokens=32``）。
   这两个数来自 LoCoMo 官方 ``gpt_utils.py`` L286-289，改一个字分数
   就不可比了。它们由 ``locomo_official`` 导出，此处只引用不重定义。
2. **密钥永不落盘。** 只从环境变量或 macOS 钥匙串取，绝不写进仓库、
   报告、日志。``describe()`` 只吐指纹前 16 位。
3. **模型路由是配置，不是猜测。** 哪个模型走哪个网关，由
   ``ROUTES`` 明确列出；没列的模型直接报错，不做「聪明」的兜底——
   走错网关会烧掉不该烧的额度。
4. **网关地址与密钥同级敏感。** 地址是运营方的私有资产，写进源码就会随
   仓库一起公开。故 :class:`Gateway` 只存「到哪里去取地址」，真地址从
   环境变量或钥匙串读——**代码里没有任何兜底的 endpoint 字面量**，
   宁可跑不起来，也不留字面量。

**实测记录（2026-08-23，写进代码是为了后人不必重测）：**

* 网关前置 Cloudflare 会按客户端指纹拦截，缺 ``User-Agent`` 一律
  ``HTTP 403 / error code: 1010``。故 :data:`_UA` 是必需请求头，不是伪装。
* 部分网关会在模型前**强行注入一段系统提示**，实测同一句 11 token 的
  问题：``gpt-4o-by-openai`` 计费 16 token（干净）、``qwen3.8-max``
  计费 2061、``claude-opus-5`` 计费 8942。注入**关不掉**（显式传
  ``system`` 只会叠加）。这不会让分数虚高——LoCoMo 用 F1，被注入的
  模型倾向啰嗦，只会扣 precision——但**必须在 RESULTS.md 里标注口径**。
* 上游偶发 ``HTTP 521``（Cloudflare「源站挂了」），实测约三分之一，
  故重试是必需品而非保险。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .locomo_official import OFFICIAL_MAX_TOKENS, OFFICIAL_TEMPERATURE

__all__ = ["AnswerModel", "AnswerError", "ROUTES", "resolve"]


# Cloudflare 按客户端指纹拦截，缺这个头是 403/1010。见模块 docstring。
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 瞬时故障：重试有意义。521 是实测最常见的那个。
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 520, 521, 522, 524})


# 钥匙串里存网关地址用的账号名（与存密钥的账号分开，互不覆盖）。
_BASE_URL_ACCOUNT = "base-url"


class AnswerError(RuntimeError):
    """答题模型调用失败（已重试到上限），或网关配置缺失。"""


def _keychain(service: str, account: str) -> str:
    """读一条 macOS 钥匙串。取不到就返回空串，由调用方给人话错误。"""
    try:
        out = subprocess.run(
            ["security", "find-generic-password",
             "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


@dataclass(frozen=True)
class Gateway:
    """一个网关：地址 + 取密钥的办法。密钥本身不在这里。"""

    name: str
    base_url_env: str
    env_var: str
    keychain_service: str
    keychain_account: str

    @property
    def base_url(self) -> str:
        """网关地址：环境变量优先，回落钥匙串；都没有就报错。

        **这里故意没有默认值。** 地址是私有资产，一旦写成字面量就会随
        仓库公开；缺配置时报错是对的，兜底才是错的。
        """
        val = (os.environ.get(self.base_url_env, "").strip()
               or _keychain(self.keychain_service, _BASE_URL_ACCOUNT))
        if not val:
            raise AnswerError(
                f"取不到 {self.name} 的网关地址：环境变量 {self.base_url_env} 为空，"
                f"钥匙串 {self.keychain_service}/{_BASE_URL_ACCOUNT} 也没有。"
                "地址不许写进仓库——请设环境变量或存钥匙串。"
            )
        return val.rstrip("/")


# 路由铁律（项目约定）：gpt 系走中转（额度有限、省着用），其余走 9r。
# 走错网关＝烧错额度，所以这里只认白名单，不做前缀猜测的兜底。
VOLINK = Gateway(
    name="volink",
    base_url_env="AIDUMEI_VOLINK_BASE_URL",
    env_var="AIDUMEI_VOLINK_API_KEY",
    keychain_service="aidumei-volink",
    keychain_account="gpt4o-relay",
)
NINER = Gateway(
    name="9r",
    base_url_env="AIDUMEI_9R_BASE_URL",
    env_var="AIDUMEI_9R_API_KEY",
    keychain_service="aidumei-9r",
    keychain_account="answer-llm",
)

ROUTES: dict[str, Gateway] = {
    "gpt-4o-by-openai": VOLINK,
    "gpt-4-1-by-openai": VOLINK,
    # qwen3.8-max：线路拥挤（实测约每三次一次 HTTP 521），且在官方 32 token
    # 上限下会把预算全花在 reasoning_content 上、content 返回空——空答案不抛
    # 异常，会被当「答错」计入均值，捏出一个假成绩。2026-08-23 定档改用
    # gemini-3.7-flash（实测 32 token 下正常出词，无思考字段污染）。
    "qwen3.8-max": NINER,
    "gemini-3.7-flash": NINER,
    "claude-opus-5": NINER,
    "claude-opus-4.8": NINER,
}


def _read_key(gw: Gateway) -> str:
    """取密钥：环境变量优先（生产机没钥匙串），回落 macOS 钥匙串。"""
    val = (os.environ.get(gw.env_var, "").strip()
           or _keychain(gw.keychain_service, gw.keychain_account))
    if val:
        return val
    raise AnswerError(
        f"取不到 {gw.name} 的密钥：环境变量 {gw.env_var} 为空，"
        f"钥匙串 {gw.keychain_service}/{gw.keychain_account} 也没有。"
        "密钥不许写进仓库或配置文件——请设环境变量或存钥匙串。"
    )


def resolve(model_id: str) -> Gateway:
    """按模型名定网关。没登记的模型直接报错，不猜。"""
    gw = ROUTES.get(model_id)
    if gw is None:
        raise AnswerError(
            f"模型 {model_id!r} 没有登记路由。已登记："
            + "、".join(sorted(ROUTES))
            + "。不给未登记模型做前缀兜底——走错网关会烧掉别处的额度。"
        )
    return gw


@dataclass
class AnswerModel:
    """答题模型。一次构造，全程复用；累计用量供成本对账。"""

    model_id: str
    gateway: Gateway = field(init=False)
    max_retries: int = 4
    timeout: float = 120.0
    # 官方口径，不给调。想改必须连 PROTOCOL.md 一起改。
    temperature: float = OFFICIAL_TEMPERATURE
    max_tokens: int = OFFICIAL_MAX_TOKENS
    calls: int = field(default=0, init=False)
    retries: int = field(default=0, init=False)
    prompt_tokens: int = field(default=0, init=False)
    completion_tokens: int = field(default=0, init=False)
    _key: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        self.gateway = resolve(self.model_id)
        self._key = _read_key(self.gateway)

    def describe(self) -> dict:
        """可以安全写进报告的自述：**不含密钥明文**，只有指纹。"""
        import hashlib

        return {
            "model": self.model_id,
            "gateway": self.gateway.name,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "key_sha256_16": hashlib.sha256(self._key.encode()).hexdigest()[:16],
        }

    def usage(self) -> dict:
        return {
            "calls": self.calls,
            "retries": self.retries,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }

    def complete(self, prompt: str) -> str:
        """发一次问答，返回模型输出的原始文本（仅 strip）。

        重试只针对**瞬时**故障（网络、以及 :data:`_RETRYABLE_STATUS` 里
        那些状态码）。401/403/404 这类是配置错，重试没意义也不重试——
        让它当场炸出来，比跑到一半才发现半份结果不可用强。
        """
        body = json.dumps({
            "model": self.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }).encode("utf-8")
        headers = {
            "Authorization": "Bearer " + self._key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _UA,
        }
        url = self.gateway.base_url.rstrip("/") + "/chat/completions"

        last = ""
        for attempt in range(self.max_retries):
            if attempt:
                self.retries += 1
                time.sleep(min(2.0 * (2 ** (attempt - 1)), 16.0))
            try:
                req = urllib.request.Request(url, data=body, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code not in _RETRYABLE_STATUS:
                    raise AnswerError(
                        f"{self.gateway.name}/{self.model_id} 返回 HTTP {exc.code}，"
                        "属配置错而非瞬时故障，不重试。"
                    ) from exc
                last = f"HTTP {exc.code}"
                continue
            except (urllib.error.URLError, TimeoutError, OSError,
                    json.JSONDecodeError) as exc:
                last = type(exc).__name__
                continue

            self.calls += 1
            u = payload.get("usage") or {}
            self.prompt_tokens += int(u.get("prompt_tokens") or 0)
            self.completion_tokens += int(u.get("completion_tokens") or 0)
            choices = payload.get("choices") or []
            if not choices:
                raise AnswerError(
                    f"{self.gateway.name}/{self.model_id} 返回了空 choices：{payload!r:.200}"
                )
            text = str(choices[0].get("message", {}).get("content") or "").strip()
            if not text:
                # 空答案必须炸，不许悄悄当「答错」摊进均值。推理模型会把
                # max_tokens 预算全花在 reasoning_content 上，导致 content 返回
                # null、finish_reason=length。此处若返回空串，答案会被判错但
                # answer_failures 仍记 0——成绩被静默压低却没有任何报错，
                # 假绿灯比红灯毒。空若是瞬时的，重试能捞回来；若是模型协议
                # 不兼容，重试到上限后抛错。两条路都不会把空答案记成答错。
                fr = (choices[0].get("finish_reason")
                      or payload.get("finish_reason") or "?")
                last = f"content 为空（finish_reason={fr}）"
                continue
            return text

        raise AnswerError(
            f"{self.gateway.name}/{self.model_id} 重试 {self.max_retries} 次仍失败，"
            f"最后一次：{last}"
        )
