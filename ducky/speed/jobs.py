"""aiduMEM speed · 异步 job 状态"""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from typing import Optional

_jobs_lock = threading.Lock()
_jobs: "OrderedDict[str, dict]" = OrderedDict()
_JOBS_MAX = 200


def job_create(payload: dict) -> str:
    job_id = uuid.uuid4().hex[:16]
    rec = {
        "job_id": job_id,
        "status": "queued",
        "created_at": time.time(),
        "updated_at": time.time(),
        "payload_preview": (payload.get("text_preview") or "")[:120],
        # v20.4.0（三方审计 P1-5 · Kimi P2-2）：job 记录带租户轴。此前记录
        # 无归属、job_get 裸 id 直查 —— 同一把门禁下的任何调用方拿到 job_id
        # 就能读到别的租户异步写入的预览与完整结果，与「所有读写路径二维
        # 作用域」的既定原则不一致。
        "user_id": str(payload.get("user_id") or "default"),
        "bank_id": str(payload.get("bank_id") or "default"),
        "result": None,
        "error": None,
    }
    with _jobs_lock:
        _jobs[job_id] = rec
        _jobs.move_to_end(job_id)
        while len(_jobs) > _JOBS_MAX:
            _jobs.popitem(last=False)
    return job_id


def job_update(job_id: str, **kwargs) -> None:
    with _jobs_lock:
        rec = _jobs.get(job_id)
        if not rec:
            return
        rec.update(kwargs)
        rec["updated_at"] = time.time()
        _jobs.move_to_end(job_id)


def job_get(job_id: str, *, user_id: Optional[str] = None,
            bank_id: Optional[str] = None) -> Optional[dict]:
    """按 id 取 job；给了 scope 就校验归属，不符按不存在处理（不泄露存在性）。

    scope 参数为 None 表示调用方没有携带租户语义（进程内部消费者），
    保持既有行为；HTTP 查询端点必须传全两轴。
    """
    with _jobs_lock:
        rec = _jobs.get(job_id)
        if not rec:
            return None
        if user_id is not None and str(rec.get("user_id") or "default") != str(user_id):
            return None
        if bank_id is not None and str(rec.get("bank_id") or "default") != str(bank_id):
            return None
        return dict(rec)
