"""f0.3 dependency pins: requirements.txt locks the production versions.

requirements.txt is the locked runtime environment ("pip install -r
requirements.txt" is the README's first path and what production runs).
Before f0.3 five entries were ranges (uvicorn, httpx, requests,
python-multipart, inotify_simple), so the same file installed different
servers on different days.  Now every entry is an exact pin, and every pin
must satisfy the corresponding pyproject.toml specifier (floor = lock), so
``pip install -r requirements.txt`` and ``pip install .`` cannot contradict
each other.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
PYPROJECT = ROOT / "pyproject.toml"

PRODUCTION = {
    "uvicorn": "0.52.3",
    "httpx": "0.28.1",
    "requests": "2.34.2",
    "python-multipart": "0.0.32",
    "inotify-simple": "2.0.1",
}

# The faaa9af spellings of the same five lines (negative control).
OLD_LINES = [
    "uvicorn>=0.30,<1.0",
    "httpx>=0.27",
    "requests>=2.31",
    "python-multipart>=0.0.9",
    'inotify_simple>=1.3.5; sys_platform == "linux"',
]


def _requirement_lines(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if line and not line.startswith(("#", "-")):
            lines.append(line)
    return lines


def _requirements(text: str) -> dict[str, Requirement]:
    return {canonicalize_name(r.name): r
            for r in (Requirement(line) for line in _requirement_lines(text))}


def _exact_version(req: Requirement) -> str | None:
    specs = list(req.specifier)
    if len(specs) == 1 and specs[0].operator == "==" and "*" not in specs[0].version:
        return specs[0].version
    return None


def _pyproject_requirements() -> list[Requirement]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    reqs = [Requirement(item) for item in data["dependencies"]]
    for group in data.get("optional-dependencies", {}).values():
        reqs.extend(Requirement(item) for item in group)
    return reqs


def _conflicts(locked: dict[str, Requirement], declared: list[Requirement]) -> list[str]:
    problems = []
    for req in declared:
        pin = locked.get(canonicalize_name(req.name))
        if pin is None:
            continue
        version = _exact_version(pin)
        if version is None:
            problems.append(f"{req.name}: requirements.txt is not an exact pin ({pin})")
        elif not req.specifier.contains(version, prereleases=True):
            problems.append(f"{req.name}: pinned {version} but pyproject declares {req}")
    return problems


def test_production_versions_are_pinned_exactly() -> None:
    locked = _requirements(REQUIREMENTS.read_text(encoding="utf-8"))
    assert {name: _exact_version(locked[name]) for name in PRODUCTION} == PRODUCTION
    assert locked["inotify-simple"].marker == Marker('sys_platform == "linux"')


def test_every_runtime_requirement_is_an_exact_pin() -> None:
    locked = _requirements(REQUIREMENTS.read_text(encoding="utf-8"))
    unpinned = sorted(name for name, req in locked.items() if _exact_version(req) is None)
    assert not unpinned, f"requirements.txt still has ranges: {unpinned}"


def test_pins_satisfy_every_pyproject_specifier() -> None:
    locked = _requirements(REQUIREMENTS.read_text(encoding="utf-8"))
    problems = _conflicts(locked, _pyproject_requirements())
    assert not problems, "\n".join(problems)


def test_negative_control_previous_lines_were_ranges() -> None:
    old = _requirements("\n".join(OLD_LINES))
    assert all(_exact_version(req) is None for req in old.values())
    flagged = {canonicalize_name(p.split(":", 1)[0]) for p in _conflicts(old, _pyproject_requirements())}
    assert flagged == set(PRODUCTION)


@pytest.mark.parametrize("declared", ["uvicorn[standard]>=0.53,<1.0", "httpx<0.28"])
def test_negative_control_checker_catches_a_contradicting_floor(declared: str) -> None:
    locked = _requirements(REQUIREMENTS.read_text(encoding="utf-8"))
    assert _conflicts(locked, [Requirement(declared)])


def test_existing_equality_guard_still_sees_the_new_pins() -> None:
    # tests/test_v20_runtime_deps_declaration.py parses requirements.txt with a
    # regex of its own; make sure the marker line keeps a parseable name.
    names = {re.split(r"[<>=!~\[;]", line, maxsplit=1)[0].strip().lower().replace("_", "-")
             for line in _requirement_lines(REQUIREMENTS.read_text(encoding="utf-8"))}
    assert set(PRODUCTION) <= names
