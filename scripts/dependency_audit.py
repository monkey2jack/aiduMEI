#!/usr/bin/env python3
"""Verify dependency declarations in both directions."""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path


def _normalize(name: str) -> str:
    name = name.strip()
    for sep in ("=", ">", "<", "[", ";", " "):
        name = name.split(sep, 1)[0]
    return name


def _declared(path: Path, section: str | None = None) -> set[str]:
    text = path.read_text(encoding="utf-8")
    if path.name == "requirements.txt":
        return {
            _normalize(line)
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    data = tomllib.loads(text)
    if section:
        deps = data.get("project", {}).get("optional-dependencies", {}).get(section, [])
    else:
        deps = data.get("project", {}).get("dependencies", [])
    return {_normalize(item) for item in deps}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    req = _declared(root / "requirements.txt")
    py = _declared(root / "pyproject.toml")
    missing_in_py = sorted(req - py)
    missing_in_req = sorted(py - req)
    if missing_in_py or missing_in_req:
        result = {
            "status": "fail",
            "requirements_only": missing_in_py,
            "pyproject_only": missing_in_req,
        }
    else:
        result = {"status": "ok", "dependencies": sorted(req & py)}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
