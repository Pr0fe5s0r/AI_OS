from __future__ import annotations

import pathlib
import sys

# Make the repo root importable so `packages.*` and `apps.*` resolve whether
# tests run in the container or on the host.
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.core.profile import Profile, read_profile_yaml  # noqa: E402

# The seed profiles, parsed straight from YAML (no DB). Tests exercise the
# engine with BOTH so nothing accidentally assumes one domain.
SOFTWARE_PROFILE: Profile = read_profile_yaml(ROOT / "profiles" / "software.yaml")
INVENTORY_PROFILE: Profile = read_profile_yaml(ROOT / "profiles" / "inventory.yaml")
