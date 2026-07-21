from __future__ import annotations

import pathlib
import re

# GREP TRIPWIRE (CI gate): domain knowledge enters the engine ONLY as data
# loaded from the profiles table. Any conditional that branches on an industry,
# a vertical name, or a hardcoded company id inside the engine (packages/) or
# the services (apps/) is a hole in the architecture and fails the build.

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCANNED = ("packages", "apps")

_PATTERNS = [
    # if industry == ... / vertical == ...
    re.compile(r"\b(industry|vertical)\s*(==|!=)"),
    re.compile(r"(==|!=)\s*[\'\"](software|inventory|retail|ecommerce|saas|fintech)[\'\"]"),
    # branching on a specific tenant
    re.compile(r"company_id\s*(==|!=)\s*[\'\"]"),
]


def _py_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for top in SCANNED:
        base = ROOT / top
        if base.exists():
            out.extend(p for p in base.rglob("*.py") if "node_modules" not in p.parts)
    return sorted(out)


def test_no_industry_or_company_branching_in_engine_or_services() -> None:
    violations: list[str] = []
    for path in _py_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.split("#", 1)[0]  # code only; comments may say anything
            for pattern in _PATTERNS:
                if pattern.search(stripped):
                    violations.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not violations, (
        "TRIPWIRE: hardcoded industry/company branching found — move it into profile data:\n"
        + "\n".join(violations)
    )
