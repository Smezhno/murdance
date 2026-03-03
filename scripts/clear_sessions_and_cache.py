"""Clear sessions and crm_cache tables. Use before testing booking flow.

Usage: from project root, PYTHONPATH=. python scripts/clear_sessions_and_cache.py
"""

import asyncio
import sys
from pathlib import Path

# Load project root so app.config is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.storage.postgres import postgres_storage


async def main() -> None:
    await postgres_storage.connect()
    try:
        r1 = await postgres_storage.execute("DELETE FROM sessions")
        r2 = await postgres_storage.execute("DELETE FROM crm_cache")
        print(f"Sessions: {r1}")
        print(f"Cache:    {r2}")
    finally:
        await postgres_storage.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
