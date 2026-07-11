#!/usr/bin/env python3
"""Import-boundary guardrail (MANDATORY).

Fails if anything under packages/core imports from a vertical (or an app).
The core is generic: it must never import, reference, or branch on any vertical.

Usage:
    python scripts/check_core_boundary.py      # exit 1 on any violation
"""
from __future__ import annotations

import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "packages" / "core"

# A core module may not import from these top-level packages.
FORBIDDEN_PREFIXES = ("verticals", "apps")


def _module_roots(node: ast.AST) -> list[tuple[str, int]]:
    """Return (imported_module, lineno) for every import statement."""
    out: list[tuple[str, int]] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            out.append((alias.name, node.lineno))
    elif isinstance(node, ast.ImportFrom):
        # ignore relative imports (level > 0) — they can't reach a vertical
        if node.level == 0 and node.module:
            out.append((node.module, node.lineno))
    return out


def scan(core_dir: pathlib.Path = CORE_DIR) -> list[str]:
    violations: list[str] = []
    for path in sorted(core_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for module, lineno in _module_roots(node):
                root = module.split(".")[0]
                if root in FORBIDDEN_PREFIXES:
                    rel = path.relative_to(REPO_ROOT)
                    violations.append(f"{rel}:{lineno}: core imports '{module}'")
    return violations


def main() -> int:
    violations = scan()
    if violations:
        print("IMPORT-BOUNDARY VIOLATION — packages/core must not import verticals/apps:")
        for v in violations:
            print(f"  - {v}")
        return 1
    print("OK: packages/core imports nothing from verticals/* or apps/*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
