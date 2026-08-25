#!/usr/bin/env python3
"""Fail when production provider selection escapes approved adapter boundaries."""

from __future__ import annotations

import ast
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
INVENTORY = pathlib.Path(__file__).with_name("inventory.json")
PROVIDER_ROOT = pathlib.Path("factory/capacity_pool/providers")
PROVIDER_NAMES = frozenset({"claude", "codex"})
SHELL_MARKERS = re.compile(r"FACTORY_WORKER_ORDER|--engine\s+(?:claude|codex)")
PYTHON_MARKERS = re.compile(
    r"FACTORY_WORKER_ORDER|FACTORY_[A-Z_]+MODEL_CMD|--engine"
)


def _python_invokes_provider(path: pathlib.Path) -> bool:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and first.value in PROVIDER_NAMES:
                return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in PROVIDER_NAMES and isinstance(getattr(node, "parent", None), ast.Call):
                return True
    if "factory.capacity_pool.providers" in source:
        return False
    return bool(PYTHON_MARKERS.search(source))


def direct_invocation_paths(root: pathlib.Path = ROOT) -> set[str]:
    found = set()
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if any(part in {".git", "runs", "__pycache__"} for part in relative.parts):
            continue
        if relative.parts[:2] == ("factory", "capacity_pool"):
            continue
        if relative.parts[:2] == ("factory", "acceptance") and str(relative) not in {
            "factory/acceptance/e2e_doctor.py",
            "factory/acceptance/phase4_live.py",
            "factory/acceptance/test_engine_live.py",
        }:
            continue
        live_test = str(relative) == "factory/acceptance/test_engine_live.py"
        if str(relative).startswith(str(PROVIDER_ROOT)) or (path.name.startswith("test_") and not live_test):
            continue
        if _python_invokes_provider(path):
            found.add(str(relative))
    for path in (root / "poll.sh", root / "live-e2e.sh"):
        if path.exists() and SHELL_MARKERS.search(path.read_text(encoding="utf-8")):
            found.add(str(path.relative_to(root)))
    return found


def validate_inventory(root: pathlib.Path = ROOT,
                       inventory_path: pathlib.Path = INVENTORY) -> list[str]:
    data = json.loads(inventory_path.read_text(encoding="utf-8"))
    debt = set(data.get("temporary_direct_invocation_debt", []))
    found = direct_invocation_paths(root)
    errors = [f"unapproved direct model invocation: {path}" for path in sorted(found - debt)]
    classified = {path for row in data.get("components", []) for path in row.get("paths", [])}
    errors += [f"debt path absent from inventory: {path}" for path in sorted(debt - classified)]
    return errors
