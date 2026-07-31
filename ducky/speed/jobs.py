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


def job_get(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        rec = _jobs.get(job_id)
        return dict(rec) if rec else None
