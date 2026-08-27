from __future__ import annotations

import pathlib
import re

# ALL Cypher lives in packages/core/graph.py — the same rule as the single LLM
# client. Any other module writing Cypher strings bypasses tenancy filtering,
# the bootstrap contract, and the one place we audit graph access.

ROOT = pathlib.Path(__file__).resolve().parents[1]
ALLOWED = ROOT / "packages" / "core" / "graph.py"
SCANNED = ("packages", "apps", "scripts")

# Cypher-specific tokens that never appear in SQL or ordinary Python. Kept
# case-sensitive: Cypher keywords are conventionally uppercase, and a
# case-insensitive scan trips on prose like "match (regex)".
_CYPHER = re.compile(
    r"\bMERGE\s*\(|\bMATCH\s*\(|\bDETACH DELETE\b|db\.index\.vector"
    r"|db\.create\.setNodeVectorProperty|CREATE CONSTRAINT|CREATE VECTOR INDEX"
)


def test_cypher_only_lives_in_core_graph() -> None:
    violations: list[str] = []
    for top in SCANNED:
        base = ROOT / top
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if path == ALLOWED:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if _CYPHER.search(code):
                    violations.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not violations, (
        "Cypher found outside packages/core/graph.py:\n" + "\n".join(violations)
    )
