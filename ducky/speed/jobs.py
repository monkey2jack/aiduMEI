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

# f0.3 (C2): idempotency claims owned by async jobs.  Kept out of the job
# record (job_get is an HTTP read surface) and outliving record eviction, so
# a job evicted from _jobs can still settle its claim when it finishes.
_job_idem: "OrderedDict[str, dict]" = OrderedDict()
_JOB_IDEM_MAX = 2000
_TERMINAL = ("done", "error")


def _result_failed(result) -> bool:
    return isinstance(result, dict) and str(result.get("status") or "").lower() in (
        "error", "failed", "partial", "repair_required")


def _settle_idempotency(binding: dict, job_id: str, status: str, fields: dict) -> None:
    """A finished job settles the claim it inherited from its /add request."""
    from ducky import idempotency
    result = fields.get("result")
    # settle_job_quietly never raises: settlement must not break the job itself.
    idempotency.settle_job_quietly(binding, ok=status == "done" and not _result_failed(result),
                                   result=result, job_id=job_id)


def job_create(payload: dict) -> str:
    from ducky.mutation_journal import scope_lock
    with scope_lock(payload.get("user_id"), payload.get("bank_id")):
        return _job_create(payload)


def _job_create(payload: dict) -> str:
    from ducky.bank_contract import make_scope
    scope = make_scope(payload.get("user_id"), payload.get("bank_id"))
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
        "user_id": scope.user_id,
        "bank_id": scope.bank_id,
        "result": None,
        "error": None,
    }
    # f0.4: acceptance is durable BEFORE the in-memory optimization or response.
    from ducky.mutation_journal import accept_job
    accept_job(payload, job_id=job_id)
    binding = payload.get("idempotency")
    with _jobs_lock:
        _jobs[job_id] = rec
        _jobs.move_to_end(job_id)
        while len(_jobs) > _JOBS_MAX:
            _jobs.popitem(last=False)
        if isinstance(binding, dict) and binding.get("key"):
            _job_idem[job_id] = dict(binding)
            while len(_job_idem) > _JOB_IDEM_MAX:
                _job_idem.popitem(last=False)
    return job_id


def job_update(job_id: str, **kwargs) -> None:
    from ducky.mutation_journal import update_job
    update_job(job_id, **kwargs)
    status = kwargs.get("status")
    binding = None
    with _jobs_lock:
        rec = _jobs.get(job_id)
        if rec:
            rec.update(kwargs)
            rec["updated_at"] = time.time()
            _jobs.move_to_end(job_id)
        if status in _TERMINAL:
            binding = _job_idem.pop(job_id, None)
    if binding:
        # Outside the lock: settlement is database I/O.
        _settle_idempotency(binding, job_id, status, kwargs)


def job_get(job_id: str, *, user_id: Optional[str] = None,
            bank_id: Optional[str] = None) -> Optional[dict]:
    """按 id 取 job；给了 scope 就校验归属，不符按不存在处理（不泄露存在性）。

    scope 参数为 None 表示调用方没有携带租户语义（进程内部消费者），
    保持既有行为；HTTP 查询端点必须传全两轴。
    """
    # The durable row is authoritative even while a stale cache survives privacy
    # deletion, and after a process restart/record eviction.
    from ducky.mutation_journal import job_record
    return job_record(job_id, user_id=user_id, bank_id=bank_id)
