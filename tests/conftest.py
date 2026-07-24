from __future__ import annotations

import pathlib
import sys

# Make the repo root importable so `packages.*` and `apps.*` resolve whether
# tests run in the container or on the host.
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.core.profile import SLOTS, Profile  # noqa: E402
from tests.fixtures.profiles import INVENTORY_SLOTS, SOFTWARE_SLOTS  # noqa: E402

# The engine ships with NO seed profiles: a real company's shape is discovered
# from its own data, and its actions are declared by the connectors it links.
# The suite still exercises the engine with BOTH of these so nothing can quietly
# start assuming one domain — that guarantee is the whole product claim, and it
# needs two industries that share no vocabulary to be worth anything.


def _profile(slots: dict) -> Profile:
    return Profile(
        company_id=slots["company_id"],
        **{s: slots.get(s) or Profile.model_fields[s].default_factory() for s in SLOTS},  # type: ignore[misc,call-arg]
    )


SOFTWARE_PROFILE: Profile = _profile(SOFTWARE_SLOTS)
INVENTORY_PROFILE: Profile = _profile(INVENTORY_SLOTS)
