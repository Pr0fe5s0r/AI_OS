#!/usr/bin/env python3
"""Seed the engine: profile rows from profiles/*.yaml.

  python scripts/seed.py

No mock/demo/fixture events of any kind — this only creates the DATA
definitions (profiles) that tell the engine what a company's work looks
like. Every event comes from a real connector (Connections tab) or a real
push (POST /api/ingest/push). An empty profile with nothing connected is
the correct resting state, not something to paper over with synthetic data.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from apps.common.context import seed_all_profiles  # noqa: E402
from packages.core import graph  # noqa: E402
from packages.core.db import Session  # noqa: E402


async def main() -> int:
    await graph.bootstrap()
    async with Session() as session:
        seeded = await seed_all_profiles(session)
        await session.commit()
        print(f"profiles: {', '.join(seeded)}")
    await graph.close_driver()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
