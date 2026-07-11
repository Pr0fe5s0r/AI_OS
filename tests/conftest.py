from __future__ import annotations

import pathlib
import sys

# Make the repo root importable so `packages.*`, `verticals.*`, `apps.*` resolve
# whether tests run in the container or on the host.
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
