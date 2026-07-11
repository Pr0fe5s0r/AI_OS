from __future__ import annotations

import pathlib
import sys

# Reuse the same AST scanner the CI script uses, so the test and the guardrail
# script can never disagree.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from check_core_boundary import scan  # noqa: E402


def test_core_does_not_import_verticals_or_apps() -> None:
    violations = scan()
    assert not violations, "core boundary violated:\n" + "\n".join(violations)
