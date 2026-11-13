"""f0.8 出站地址校验（SSRF 收口）：保存配置、/config/test、每次外呼前共用一套判定。

外审（f0.7+ 第二轮）指出旧版 `_reject_private_endpoint` 只看 IP 字面量：
域名（`10.0.0.5.nip.io`、自控域名）、尾点写法（`localhost.`、`169.254.169.254.`、
`metadata.google.internal.`）、`2130706433.` 一类整数/八进制加尾点全部放行；而且只在
PUT /config 时查一次，/config/test 与运行时外呼都不复检。

本模块的判定：
1. 只收 http(s)。主机名先去掉**恰一个**尾点再判，仍以点结尾直接拒。
2. 云元数据主机名/地址（`*.internal`、`metadata`、169.254.169.254、阿里云 100.100.100.200、
   fd00:ec2::254 等）永远拒，放行名单也放不过。
3. 每一段都是数字写法（十进制、0x 十六进制、0 开头八进制）却解析不成 IP → fail-closed 拒。
4. IP 字面量与 DNS 解析出的**每一个**地址都过同一套私网判定：私网、回环、链路本地、保留、
   未指定、组播、CGNAT 100.64/10、IPv4 映射/兼容/6to4/Teredo 内嵌的 IPv4。
5. 回环：字面 `127.0.0.1` / `::1` / `localhost` 默认放行（本机 vLLM/Ollama 是常见形态），
   `AIDUMEI_EGRESS_ALLOW_LOOPBACK=0` 关掉；其他域名解析到回环（`127.0.0.1.nip.io`）照拒。
6. 明文 http 只许回环（保存期规则；运行时只看目的地址，不因 scheme 打断存量配置）。
7. 自托管局域网模型机：`AIDUMEI_MODEL_ENDPOINT_ALLOW` 显式点名（主机名、IP 或 CIDR，逗号分隔）。
   名单内主机跳过私网判定（元数据地址除外）。

8. f0.8+（外审 R3-07）**钉住首次解析**：``guard_url`` 把通过校验的那组地址按主机名钉在当前线程
   （默认 30 秒）；进程里的 ``socket.getaddrinfo`` 换成一层薄包装，钉住期间同一线程再解析这个主机名
   只会拿到钉住的地址——requests/urllib3、httpx 同步客户端都在调用线程里解析，DNS rebinding 的
   第二次解析换不了地址。校验时解析失败的主机名钉成空集（连接直接失败，不再给第二次解析机会）。
   TLS 仍按原主机名做 SNI 与证书校验（URL 不改写，只换解析结果）。
9. mem0 SDK 内部的 openai 客户端：初始化后给它底层的 httpx 客户端挂 request 钩子，每次外呼前同样
   ``guard_url`` 并钉住（``attach_httpx_guard``）；挂不上（SDK 版本变了）就告警，启动时的存量配置校验照旧。

已知局限（如实写进 SECURITY.md）：钉住只对在**同一线程**里先 guard 再连的调用有效——异步客户端在
线程池里解析不受保护（本仓外呼点目前全是同步客户端）；mem0 SDK 钩子挂不上时退回「只告警」。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import threading
import time
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("aiduMEM.egress")

_METADATA_HOSTS = frozenset((
    "metadata", "metadata.google.internal", "metadata.goog", "instance-data",
    "instance-data.ec2.internal", "metadata.azure.com", "metadata.tencentyun.com",
))
_METADATA_IPS = frozenset(ipaddress.ip_address(x) for x in (
    "169.254.169.254", "169.254.170.2", "169.254.169.123", "100.100.100.200",
    "fd00:ec2::254", "fd00:ec2::23",
))
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")
_LOOPBACK_NAMES = frozenset(("localhost", "127.0.0.1", "::1"))

Resolver = Callable[[str], Iterable[str]]


class EgressBlocked(RuntimeError):
    """外呼目的地址被拦。消息里只有原因与主机名，不含凭据。"""


def _unpinned_getaddrinfo():
    """校验自己的解析绕过钉住层（否则一次解析失败钉成空集后，复检永远解析不到）。"""
    fn = socket.getaddrinfo
    while getattr(fn, "_aidumei_orig", None) is not None:
        fn = fn._aidumei_orig
    return fn


_DNS_CACHE: dict = {}
_DNS_CACHE_MAX = 256


def _dns_cache_ttl() -> float:
    """AIDUMEI_EGRESS_DNS_CACHE_S（默认 60，0 关）。

    f0.8+ C：外呼前复检每次都做一次 getaddrinfo。生产机的解析器（tailnet DNS）
    每次 A+AAAA 合计 ≈1.0s 且不缓存 —— 于是每个 rerank / LLM / decision 外呼都
    白付 1 秒（rerank 均值 0.9s → 2.2s 正是 f0.8 上线那天）。成功的解析结果短时
    复用；解析失败不缓存。每次复检仍逐地址校验，并把本次使用的缓存结果钉给真实连接。"""
    raw = os.environ.get("AIDUMEI_EGRESS_DNS_CACHE_S", "").strip()
    try:
        v = float(raw) if raw else 60.0
    except ValueError:
        return 60.0
    return min(max(v, 0.0), 3600.0)


def _default_resolver(host: str) -> list[str]:
    ttl = _dns_cache_ttl()
    now = time.monotonic()
    if ttl > 0:
        hit = _DNS_CACHE.get(host)
        if hit is not None and now - hit[0] <= ttl:
            return list(hit[1])
    infos = _unpinned_getaddrinfo()(host, None, proto=socket.IPPROTO_TCP)
    addrs = [info[4][0] for info in infos]
    if ttl > 0 and addrs:
        if len(_DNS_CACHE) >= _DNS_CACHE_MAX:
            _DNS_CACHE.clear()
        _DNS_CACHE[host] = (now, tuple(addrs))
    return addrs


_resolver: Resolver = _default_resolver


def set_resolver(fn: Optional[Resolver]) -> None:
    """测试注入点：None 恢复系统解析器。"""
    global _resolver
    _resolver = fn or _default_resolver
    _DNS_CACHE.clear()


def normalize_host(host: str) -> Optional[str]:
    """小写、去方括号、去恰一个尾点；仍以点结尾或为空返回 None。"""
    h = (host or "").strip().strip("[]").lower()
    if h.endswith("."):
        h = h[:-1]
    if not h or h.endswith(".") or h.startswith("."):
        return None
    return h


def parse_ip(host: str):
    """IP 的任一写法 → ip_address；全数字段却解析失败 → ValueError（fail-closed）；域名 → None。"""
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    labels = host.split(".")
    if all(_NUMERIC_LABEL.match(lb) for lb in labels):
        try:
            return ipaddress.ip_address(socket.inet_aton(host))
        except OSError as exc:
            raise ValueError(f"数字形态主机名无法解析为 IP: {host}") from exc
    return None


def _embedded_v4(ip):
    """IPv6 里内嵌的 IPv4（映射、兼容、6to4、Teredo）。"""
    if ip.version != 6:
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    packed = ip.packed
    if ip in _NAT64:
        return ipaddress.IPv4Address(packed[12:])  # 64:ff9b::a.b.c.d（NAT64 众所周知前缀，f0.8.0 自审）
    if packed[:12] == b"\x00" * 12 and packed[12:] not in (b"\x00\x00\x00\x00", b"\x00\x00\x00\x01"):
        return ipaddress.IPv4Address(packed[12:])  # ::a.b.c.d（已废弃的兼容写法）
    return None


def is_metadata_ip(ip) -> bool:
    inner = _embedded_v4(ip)
    return ip in _METADATA_IPS or (inner is not None and inner in _METADATA_IPS)


def ip_block_reason(ip, *, allow_loopback: bool) -> Optional[str]:
    if is_metadata_ip(ip):
        return "云元数据地址"
    inner = _embedded_v4(ip)
    candidates = [ip] + ([inner] if inner is not None else [])
    for c in candidates:
        if c.is_loopback:
            if allow_loopback:
                continue
            return "回环地址"
        if c.version == 4 and c in _CGNAT:
            return "CGNAT/tailnet 地址"
        if c.is_private or c.is_link_local or c.is_reserved or c.is_unspecified or c.is_multicast:
            return "私网/保留地址"
        if c.version == 6 and c in ipaddress.ip_network("fec0::/10"):
            return "私网/保留地址"
    return None


def _allow_entries() -> tuple[set, list]:
    names, nets = set(), []
    for raw in os.environ.get("AIDUMEI_MODEL_ENDPOINT_ALLOW", "").split(","):
        e = raw.strip().lower()
        if not e:
            continue
        try:
            nets.append(ipaddress.ip_network(e.strip("[]"), strict=False))
            continue
        except ValueError:
            pass
        n = normalize_host(e)
        if n:
            names.add(n)
    return names, nets


def _loopback_allowed() -> bool:
    return os.environ.get("AIDUMEI_EGRESS_ALLOW_LOOPBACK", "1").strip().lower() not in ("0", "false", "no", "off")


def _allowlisted(host: str, ip, names: set, nets: list) -> bool:
    if host in names:
        return True
    return ip is not None and any(ip in n for n in nets)


def check_host(raw_host: str, *, scheme: str = "https", require_https: bool = True,
               resolve: bool = True, resolved: Optional[list] = None) -> Optional[str]:
    """返回拒绝原因；None = 放行。``resolved`` 给了就把**通过校验**的解析地址追加进去
    （域名才有；IP 字面量与 localhost 不追加；解析失败时追加不了 = 空）。"""
    host = normalize_host(raw_host)
    if host is None:
        return f"主机名不合法: {raw_host!r}"
    if host in _METADATA_HOSTS or host.endswith(".internal"):
        return "云元数据/内部地址禁止作为外呼端点"
    try:
        ip = parse_ip(host)
    except ValueError as exc:
        return str(exc)
    if ip is not None and is_metadata_ip(ip):
        return "云元数据地址禁止作为外呼端点"
    names, nets = _allow_entries()
    allow_lb = _loopback_allowed()
    # 只认规范写法的回环（127.x.y.z 点分、::1、localhost）；2130706433 / 0177.0.0.1 这类变体不算
    literal_loopback = host in _LOOPBACK_NAMES or (ip is not None and ip.is_loopback and host == str(ip))
    if _allowlisted(host, ip, names, nets):
        return _check_resolved_metadata(host, ip, resolve, resolved)
    if require_https and scheme == "http" and not (literal_loopback and allow_lb):
        return "明文 http 仅允许回环（127.0.0.1/localhost）；远程端点必须 https"
    if host == "localhost":
        return None if allow_lb else "回环地址禁止作为外呼端点（AIDUMEI_EGRESS_ALLOW_LOOPBACK=0）"
    if ip is not None:
        why = ip_block_reason(ip, allow_loopback=allow_lb and literal_loopback)
        return f"{why}禁止作为外呼端点: {host}" if why else None
    if not resolve:
        return None
    try:
        addrs = list(_resolver(host))
    except (OSError, UnicodeError, ValueError):
        return None  # 解析不了就连不上；不在这里制造误杀
    return _check_public_addresses(host, addrs, names, nets, resolved)


def _check_public_addresses(host, addrs, names, nets, resolved) -> Optional[str]:
    ok = []
    for a in addrs:
        try:
            rip = ipaddress.ip_address(str(a).split("%", 1)[0])
        except ValueError:
            continue
        if _allowlisted(host, rip, names, nets) and not is_metadata_ip(rip):
            ok.append(str(a))
            continue
        why = ip_block_reason(rip, allow_loopback=False)
        if why:
            return f"{host} 解析到{why} {rip}，禁止作为外呼端点"
        ok.append(str(a))
    if resolved is not None:
        resolved.extend(ok)
    return None


def _check_resolved_metadata(host: str, ip, resolve: bool, resolved: Optional[list] = None) -> Optional[str]:
    if ip is not None or not resolve:
        return None
    try:
        addrs = list(_resolver(host))
    except (OSError, UnicodeError, ValueError):
        return None
    ok = []
    for a in addrs:
        try:
            if is_metadata_ip(ipaddress.ip_address(str(a).split("%", 1)[0])):
                return f"{host} 解析到云元数据地址，放行名单也不放行"
        except ValueError:
            continue
        ok.append(str(a))
    if resolved is not None:
        resolved.extend(ok)
    return None


def check_url(url: str, *, require_https: bool = True, resolve: bool = True,
              resolved: Optional[list] = None) -> Optional[str]:
    """校验一个外呼 URL。require_https=False 用于运行时复检（只看目的地址）。"""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError as exc:
        return f"URL 无法解析: {exc}"
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return "只接受 http(s) URL"
    if parts.username or parts.password:
        return "URL 不得内嵌账号口令"
    try:
        _ = parts.port
    except ValueError:
        return "端口不合法"
    raw_host = parts.hostname or ""
    if not raw_host:
        return "无法解析主机名"
    return check_host(raw_host, scheme=scheme, require_https=require_https, resolve=resolve,
                      resolved=resolved)


# ── R3-07：钉住首次解析（线程内、按主机名、短 TTL）────────────────────────────
_PIN_TTL_S = 30.0
_pins = threading.local()
_hook_lock = threading.Lock()


def _pin_key(host: str) -> Optional[str]:
    h = normalize_host(host if isinstance(host, str) else (host or b"").decode("ascii", "ignore"))
    if h is None:
        return None
    try:
        return h.encode("idna").decode("ascii")
    except UnicodeError:
        return h


def _pin_table() -> dict:
    t = getattr(_pins, "table", None)
    if t is None:
        t = _pins.table = {}
    return t


def pin_host(host: str, addrs: Iterable[str], ttl: float = _PIN_TTL_S) -> None:
    """当前线程里把 host 的解析结果钉成 addrs（空 = 解析失败）。"""
    key = _pin_key(host)
    if key:
        _pin_table()[key] = (time.monotonic() + ttl, [str(a).split("%", 1)[0] for a in addrs])
        _install_getaddrinfo_hook()


def clear_pins() -> None:
    _pin_table().clear()


def _pinned(host) -> Optional[list]:
    try:
        key = _pin_key(host)
    except (TypeError, ValueError, AttributeError):
        return None
    if not key:
        return None
    hit = _pin_table().get(key)
    if hit is None:
        return None
    if hit[0] < time.monotonic():
        _pin_table().pop(key, None)
        return None
    return hit[1]


def _make_hook(orig):
    def _aidumei_pinned_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        addrs = _pinned(host) if host else None
        if addrs is None:
            return orig(host, port, family, type, proto, flags)
        if not addrs:
            raise socket.gaierror(socket.EAI_NONAME, "egress pin: host did not resolve at check time")
        out = []
        for a in addrs:
            try:
                out.extend(orig(a, port, family, type, proto, flags | socket.AI_NUMERICHOST))
            except socket.gaierror:
                continue  # 比如要 AF_INET 而这条是 IPv6
        if not out:
            raise socket.gaierror(socket.EAI_NONAME, "egress pin: no pinned address for requested family")
        return out
    _aidumei_pinned_getaddrinfo._aidumei_orig = orig  # type: ignore[attr-defined]
    return _aidumei_pinned_getaddrinfo


def _install_getaddrinfo_hook() -> None:
    """socket.getaddrinfo 换成薄包装（幂等；被别人换掉了就包住新的那个）。未钉住时原样透传。"""
    with _hook_lock:
        cur = socket.getaddrinfo
        if getattr(cur, "_aidumei_orig", None) is None:
            socket.getaddrinfo = _make_hook(cur)


def _host_of(url: str) -> str:
    try:
        return urlsplit(str(url or "").strip()).hostname or ""
    except ValueError:
        return ""


def guard_url(url: str, purpose: str = "egress") -> str:
    """外呼前一刻复检；被拦抛 EgressBlocked（RuntimeError 子类，各调用点既有降级路径接得住）。
    放行时把这次校验过的解析结果钉在当前线程（R3-07），紧接着的真实连接只会连到这些地址。"""
    resolved: list = []
    why = check_url(url, require_https=False, resolved=resolved)
    if why:
        logger.warning("egress blocked (%s): %s", purpose, why)
        raise EgressBlocked(f"egress blocked ({purpose}): {why}")
    host = normalize_host(_host_of(url)) or ""
    literal = host == "localhost" or _is_ip_literal(host)
    if host and not literal:
        pin_host(host, resolved)
    return url


def _is_ip_literal(host: str) -> bool:
    try:
        return parse_ip(host) is not None
    except ValueError:
        return True


def attach_httpx_guard(client, purpose: str) -> bool:
    """给一个 httpx（同步）客户端挂 request 钩子：每次发请求前 guard_url + 钉住解析。
    用于不经本仓外呼点的 SDK（mem0 内部的 openai 客户端）。挂上返回 True。"""
    hooks = getattr(client, "event_hooks", None)
    if not isinstance(hooks, dict):
        return False
    req_hooks = list(hooks.get("request") or [])
    if any(getattr(h, "_aidumei_guard", False) for h in req_hooks):
        return True

    def _guard(request):
        guard_url(str(request.url), purpose)
    _guard._aidumei_guard = True  # type: ignore[attr-defined]
    try:
        client.event_hooks = {**hooks, "request": req_hooks + [_guard]}
    except (AttributeError, TypeError):
        return False
    return True


def audit_config_endpoints(raw_cfg: dict) -> list[str]:
    """启动时对存量配置（手改文件、备份还原）做一次校验，返回告警行（不含密钥）。"""
    out = []
    for section in ("llm", "embedder", "rerank", "vision", "decision"):
        cfg = ((raw_cfg or {}).get(section) or {}).get("config") or {}
        ep = str(cfg.get("openai_base_url") or "").strip()
        if ep:
            why = check_url(ep, require_https=False)
            if why:
                out.append(f"{section}.openai_base_url: {why}")
    vs = ((raw_cfg or {}).get("vector_store") or {}).get("config") or {}
    host = str(vs.get("host") or "").strip()
    if host:
        why = (check_url(host, require_https=False) if "://" in host
               else check_host(host, require_https=False))
        if why:
            out.append(f"vector_store.host: {why}")
    return out
