"""Fresh-process validation used after replacing official resources; no DB writes."""
from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3

from gateway.domain import Catalog
from gateway.runtime import RuntimeRegistry
from gateway.security import SecretVault
from gateway.store import read_catalog


async def validate(root: Path):
    database = root / "data" / "gateway.sqlite3"
    current = Catalog()
    if database.exists():
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            current = read_catalog(db)
    registry = RuntimeRegistry(root, None, SecretVault(root / "data"))
    registry.current = await registry.build(current)
    await registry.close()


if __name__ == "__main__":
    import sys
    asyncio.run(validate(Path(sys.argv[1]).resolve()))
