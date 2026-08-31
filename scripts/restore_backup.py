#!/usr/bin/env python3
"""Restore points from a Qdrant backup snapshot through the live API.

This script is deliberately explicit: it does not discover a "likely" backup,
because restore is a destructive operation. It requires the exact snapshot file
created by the vector backend's snapshot operation.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import requests

_REPO = os.environ.get("AIDUMEM_HOME") or str(Path(__file__).resolve().parents[1])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

try:
    from qdrant_client import QdrantClient
except ImportError as exc:  # pragma: no cover - depends on optional deployment extras
    raise SystemExit(
        f"restore_backup.py requires qdrant-client: {exc}\n"
        "Install project dependencies before running a restore."
    )

from ducky.utils import api_auth_headers as _auth_headers  # noqa: E402

DEFAULT_API = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read a Qdrant storage.sqlite snapshot and replay its points through /add.",
    )
    parser.add_argument(
        "snapshot",
        type=Path,
        help="Exact path to the storage.sqlite snapshot produced by the vector backend.",
    )
    parser.add_argument("--api", default=DEFAULT_API, help="Live aiduMEI API base URL")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and count points without writing to the live API.",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Restore at most N points (0 means all).",
    )
    return parser.parse_args()


def _read_points(snapshot: Path) -> list[dict[str, Any]]:
    if not snapshot.is_file():
        raise FileNotFoundError(f"Snapshot not found: {snapshot}")
    if snapshot.name != "storage.sqlite":
        raise ValueError(
            "Expected a storage.sqlite snapshot; refusing an arbitrary file. "
            f"Got: {snapshot.name}"
        )

    temporary = tempfile.mkdtemp(prefix="aidumem_restore_")
    temporary_path = Path(temporary)
    try:
        qdrant_root = temporary_path / "qdrant"
        collection_dir = qdrant_root / "collection" / "mem0"
        collection_dir.mkdir(parents=True)
        shutil.copy(snapshot, collection_dir / "storage.sqlite")
        client = QdrantClient(path=str(qdrant_root))
        points = []
        offset = None
        while True:
            found, next_offset = client.scroll(
                collection_name="mem0",
                limit=500,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            points.extend(found)
            if next_offset is None:
                break
            offset = next_offset
        client.close()
        return points
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _restore(points: list[dict[str, Any]], api: str, dry_run: bool, limit: int) -> tuple[int, int]:
    if dry_run:
        return len(points), 0
    selected = points if limit <= 0 else points[:limit]
    success = failed = 0
    for index, point in enumerate(selected, 1):
        payload = point.payload or {}
        content = payload.get("data", "")
        if not content:
            failed += 1
            continue
        body = {
            "messages": [{"role": "user", "content": content}],
            "user_id": payload.get("user_id", "default"),
        }
        try:
            response = requests.post(
                f"{api}/add", json=body, timeout=30, headers=_auth_headers()
            )
            if response.status_code == 200:
                success += 1
            else:
                failed += 1
                if failed <= 3:
                    print(f"  X [{response.status_code}]: {response.text[:120]}")
        except requests.RequestException as exc:
            failed += 1
            if failed <= 3:
                print(f"  X: {exc}")
        if index % 200 == 0:
            print(f"  progress: {index}/{len(selected)}")
    return success, failed


def main() -> int:
    args = _parse_args()
    points = _read_points(args.snapshot)
    print(f"snapshot points: {len(points)}")
    if args.limit > 0:
        print(f"restore limit: {args.limit}")
    success, failed = _restore(points, args.api, args.dry_run, args.limit)
    mode = "dry-run" if args.dry_run else "restored"
    print(f"{mode}: success={success}, failed={failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
