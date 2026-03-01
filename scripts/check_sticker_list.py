#!/usr/bin/env python3
"""One-off: check if Impulse CRM API has sticker/list and what it returns.
Run from project root: python scripts/check_sticker_list.py
"""
import asyncio
import json
import sys
from pathlib import Path

# project root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.integrations.impulse.client import ImpulseClient


async def main():
    client = ImpulseClient()
    try:
        # Same pattern as schedule/list, group/list
        data = await client.list("sticker", limit=5, page=1)
        print("sticker/list: OK")
        print("Type:", type(data))
        print("Length:", len(data) if isinstance(data, list) else "N/A")
        print("Sample (first item):")
        if isinstance(data, list) and data:
            print(json.dumps(data[0], ensure_ascii=False, indent=2))
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        print("sticker/list: FAILED")
        print(type(e).__name__, str(e))
        raise
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
