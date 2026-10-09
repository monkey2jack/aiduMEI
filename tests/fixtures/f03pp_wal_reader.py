# Extracted VERBATIM from f0.3++ reader. Test fixture only; never runtime code.
# Source commit: d317d07860ae25ecb841f0a9c964fdd63b877899
# Full original module SHA256: 97c4ac43f355be750bd7f58d1a44a5a583912f40c2bdb4d27d27596e60f7e187
from __future__ import annotations
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Literal, Optional
import json
import time
import uuid
from ducky.bank_contract import DEFAULT_BANK_ID
from ducky.wal_engine import WALIntegrityError, _file_lock

@dataclass
class WALEntry:
    wal_id: str = field(default_factory=lambda: f"wal-{uuid.uuid4().hex[:12]}")
    timestamp: float = field(default_factory=time.time)
    user_id: str = "default"
    bank_id: str = DEFAULT_BANK_ID
    operation: Literal["add", "delete", "delete_all", "update", "refine"] = "add"
    payload: Dict[str, Any] = field(default_factory=dict)
    status: Literal["pending", "committed", "failed"] = "pending"
    error: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> Optional[WALEntry]:
        try:
            d = json.loads(line.strip())
            return cls(**d)
        except Exception:
            return None


class WALEngine:
    def get_pending_entries(self) -> List[WALEntry]:
        """读取所有未提交的有效操作。

        A malformed row or read failure is an integrity failure, never an empty
        pending list.  Returning ``[]`` here would allow startup reconciliation
        to report a healthy ledger while an operation is hidden behind a bad row.
        """
        entries_by_id: Dict[str, WALEntry] = {}
        status_updates: Dict[str, str] = {}

        with _file_lock(self.lock_file, exclusive=False):
            try:
                try:
                    self.wal_file.stat()
                except FileNotFoundError:
                    return []
                with open(self.wal_file, encoding="utf-8") as f:
                    for line_no, line in enumerate(f, 1):
                        entry = WALEntry.from_json(line)
                        if not entry:
                            if line.strip():
                                raise WALIntegrityError(
                                    f"WAL 第 {line_no} 行无法解析；原文保留，暂停自动对账"
                                )
                            continue
                        if entry.payload.get("target_wal_id"):
                            status_updates[entry.payload["target_wal_id"]] = entry.payload.get("updated_status", "")
                        else:
                            entries_by_id[entry.wal_id] = entry
            except WALIntegrityError:
                raise
            except (OSError, UnicodeError) as exc:
                raise WALIntegrityError(f"读取 WAL 失败: {exc}") from exc

        pending = []
        for wid, ent in entries_by_id.items():
            final_status = status_updates.get(wid, ent.status)
            if final_status == "pending":
                pending.append(ent)
        return pending

