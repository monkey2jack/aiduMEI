"""ducky.router_usage — 从上游 LLM 路由网关抓取真实用量数据（可选功能）

本模块通过 SSH 读取一台运行 LLM 路由网关的主机上的
SQLite 用量库，把每日 prompt/completion token 数汇总回来，供 /usage 类端点展示。

属于纯可选的增强能力：**默认关闭**（v19.4.1 P2-4），且所有连接参数都从环境
变量读取，任一项缺失即直接跳过，不影响 aiduMEM 主链路。需要启用时配置：

    AIDUMEM_ROUTER_USAGE_ENABLED=1   总开关，不设则本模块完全不执行
    AIDUMEM_ROUTER_SSH_HOSTS   逗号分隔的 ssh 目标，按顺序尝试，如 "user@10.0.0.2,user@203.0.113.5"
    AIDUMEM_ROUTER_SSH_KEY     ssh 私钥路径
    AIDUMEM_ROUTER_DB_PATH     远端用量 SQLite 路径，默认 ~/router-data/db/data.sqlite
    AIDUMEM_ROUTER_KEY_SUFFIX  只统计 apiKey 以此结尾的调用，留空表示不过滤
    AIDUMEM_ROUTER_MODELS      逗号分隔的模型白名单，留空表示统计全部模型
"""
import base64
import json
import logging
import os
import subprocess
from typing import Any, Dict, List

logger = logging.getLogger("aiduMEM.router_usage")

DEFAULT_REMOTE_DB = "~/router-data/db/data.sqlite"


def _ssh_hosts() -> List[str]:
    raw = os.environ.get("AIDUMEM_ROUTER_SSH_HOSTS", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def _models() -> List[str]:
    raw = os.environ.get("AIDUMEM_ROUTER_MODELS", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


def _build_remote_script() -> str:
    """生成在远端执行的查询脚本。

    参数全部通过 SQL 占位符传入，不做字符串拼接，避免注入。
    """
    db_path = os.environ.get("AIDUMEM_ROUTER_DB_PATH", DEFAULT_REMOTE_DB)
    key_suffix = os.environ.get("AIDUMEM_ROUTER_KEY_SUFFIX", "").strip()
    models = _models()

    where, params = [], []
    if key_suffix:
        where.append("apiKey LIKE ?")
        params.append(f"%{key_suffix}")
    if models:
        where.append("model IN (%s)" % ",".join("?" for _ in models))
        params.extend(models)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    sql = (
        "SELECT date(timestamp), count(*), sum(promptTokens), sum(completionTokens) "
        "FROM usageHistory" + where_sql +
        " GROUP BY date(timestamp) ORDER BY date(timestamp)"
    )

    payload = json.dumps({"db": db_path, "sql": sql, "params": params})
    return (
        "import sqlite3, json, os\n"
        f"cfg = json.loads({payload!r})\n"
        "conn = sqlite3.connect(os.path.expanduser(cfg['db']))\n"
        "rows = conn.execute(cfg['sql'], cfg['params']).fetchall()\n"
        "res = {r[0]: {'calls': r[1], 'input_tokens': r[2] or 0, "
        "'output_tokens': r[3] or 0, "
        "'total_tokens': (r[2] or 0) + (r[3] or 0)} for r in rows}\n"
        "print(json.dumps(res))\n"
    )


def router_usage_enabled() -> bool:
    """本模块是否启用（P2-4 v19.4.1：默认关闭，必须显式开启）。

    为什么加这道开关：
        本模块通过 `ssh` 在远端主机上执行 `python3 -c "exec(base64...)"`。
        SQL 已参数化、脚本内容由本地生成而非外部输入，实际注入面为零，
        但 `exec(base64_decode(...))` 这个形态会在任何开源安全审查
        （以及用户的第一次通读）里触发红灯，需要读者花时间自证无害。
        它又只是一个可选的运维观测能力，与记忆主链路无关。

        因此改为**默认不加载不执行**：需要的部署方显式设
        `AIDUMEM_ROUTER_USAGE_ENABLED=1`，并自行确认其信任前提
        （远端主机可信、ssh 私钥可信、known_hosts 已预置）。
    """
    return os.environ.get("AIDUMEM_ROUTER_USAGE_ENABLED", "0").strip().lower() in {
        "1", "true", "yes",
    }


def fetch_router_llm_usage() -> Dict[str, Any]:
    """按日汇总上游网关的 LLM token 用量；未启用/未配置/全部失败均返回 {}。"""
    if not router_usage_enabled():
        logger.debug("router_usage 未启用（设 AIDUMEM_ROUTER_USAGE_ENABLED=1 开启），跳过")
        return {}
    hosts = _ssh_hosts()
    ssh_key = os.environ.get("AIDUMEM_ROUTER_SSH_KEY", "")
    if not hosts or not ssh_key:
        logger.debug("未配置 AIDUMEM_ROUTER_SSH_HOSTS / AIDUMEM_ROUTER_SSH_KEY，跳过上游用量抓取")
        return {}

    b64_script = base64.b64encode(_build_remote_script().encode()).decode()
    remote_cmd = (
        "python3 -c \"import base64; "
        f"exec(base64.b64decode('{b64_script}').decode())\""
    )

    for host in hosts:
        cmd = [
            # accept-new 会在首次连接时接受任意主机密钥（TOFU）。
            # 生产环境建议预置 known_hosts 后改为 StrictHostKeyChecking=yes，
            # 可通过 AIDUMEM_ROUTER_SSH_STRICT=yes 覆盖。
            "ssh",
            "-o", f"StrictHostKeyChecking={os.environ.get('AIDUMEM_ROUTER_SSH_STRICT', 'accept-new')}",
            "-o", "ConnectTimeout=3",
            "-i", ssh_key,
            host,
            remote_cmd,
        ]
        try:
            res = subprocess.check_output(cmd, timeout=5).decode().strip()
            if res:
                return json.loads(res)
        except Exception as e:
            logger.debug(f"从上游网关 [{host}] 抓取 LLM 用量尝试失败: {e}")
            continue

    logger.warning("上游网关所有节点抓取 LLM 用量均失败")
    return {}
