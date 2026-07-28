from __future__ import annotations

import pathlib
import sys

import pytest

# Make the repo root importable so `packages.*` and `apps.*` resolve whether
# tests run in the container or on the host.
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.shared.schema import Scope  # noqa: E402

# Every test works inside an explicit throwaway scope. Nothing in the suite may
# touch a real workspace, and nothing may rely on a default tenant existing —
# tenancy is the one property the KB cannot get wrong, so the tests carry it
# deliberately rather than inheriting it.

TENANT = "test-agency"
OTHER_TENANT = "other-agency"

SCOPE = Scope(tenant_id=TENANT)
BRAND_A = Scope(tenant_id=TENANT, brand_id="brand-a")
BRAND_B = Scope(tenant_id=TENANT, brand_id="brand-b")
OTHER = Scope(tenant_id=OTHER_TENANT)


def _database_reachable() -> bool:
    """One connection attempt, cached, so the whole suite pays it once."""
    global _DB_OK
    if _DB_OK is None:
        import asyncio

        from sqlalchemy import text

        from packages.core.db import Session

        async def probe() -> bool:
            try:
                async with Session() as session:
                    await session.execute(text("SELECT 1"))
                return True
            except Exception:
                return False

        try:
            _DB_OK = asyncio.run(probe())
        except Exception:
            _DB_OK = False
    return _DB_OK


_DB_OK: bool | None = None


def pytest_collection_modifyitems(config, items):
    """Skip database tests when there is no database, rather than failing them.

    A missing local stack is not a defect in the code under test, and a suite
    that reports seven red lines for it trains people to ignore red lines.
    """
    if _database_reachable():
        return
    skip = pytest.mark.skip(reason="needs Postgres (docker compose up)")
    for item in items:
        if "needs_db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
async def db():
    """A session against a live Postgres, or skip.

    Skipping is deliberate rather than mocked: the write path's guarantees —
    the one-active-version index, ON CONFLICT behaviour, tsvector ranking —
    are enforced by Postgres itself, so a mock would prove nothing about the
    thing under test.
    """
    from sqlalchemy import text

    from packages.core.db import Session

    try:
        async with Session() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # no database on this host
        pytest.skip(f"needs Postgres: {type(exc).__name__}")

    async with Session() as session:
        yield session


@pytest.fixture(autouse=True)
async def clean_scope(request):
    """Remove anything the tests wrote, in both directions.

    Test data lives under throwaway tenant ids that no real workspace uses, and
    is cleared before and after so a failed run cannot poison the next one.
    """
    if "db" not in request.fixturenames:
        yield
        return

    from sqlalchemy import text

    from packages.core.db import Session

    async def purge():
        try:
            async with Session() as session:
                await session.execute(
                    text("DELETE FROM kb_items WHERE tenant_id = ANY(:t)"),
                    {"t": [TENANT, OTHER_TENANT]},
                )
                await session.execute(
                    text("DELETE FROM brands WHERE tenant_id = ANY(:t)"),
                    {"t": [TENANT, OTHER_TENANT]},
                )
                await session.commit()
        except Exception:
            pass

    await purge()
    yield
    await purge()
