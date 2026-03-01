#!/usr/bin/env python3
"""Check schedule/list and sticker/list API responses.
Run from project root: python scripts/check_schedule_and_sticker_api.py
Uses CRM_TENANT and CRM_API_KEY from .env (same as app).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings

try:
    import httpx
except ImportError:
    print("pip install httpx")
    sys.exit(1)


def main() -> None:
    s = get_settings()
    base = f"https://{s.crm_tenant}.impulsecrm.ru/api/public"
    headers = {
        "Authorization": f"Basic {s.crm_api_key}",
        "Content-Type": "application/json",
    }

    def post(url_base: str, path: str, body: dict) -> dict:
        r = httpx.post(f"{url_base}{path}", json=body, headers=headers, timeout=30.0)
        r.raise_for_status()
        return r.json()

    print("=" * 60)
    print("1. schedule/list (limit=3)")
    print("=" * 60)
    try:
        data = post(base, "/schedule/list", {"limit": 3})
        text = json.dumps(data, ensure_ascii=False, indent=2)
        lines = text.splitlines()
        for line in lines[:80]:
            print(line)
        if len(lines) > 80:
            print(f"... ({len(lines) - 80} more lines)")
        # Check first item keys
        items = data.get("items", data.get("data", data if isinstance(data, list) else []))
        if isinstance(items, list) and items:
            first = items[0]
            has_sticker = "sticker" in first or "stickers" in first
            print("\nFirst item keys:", list(first.keys()))
            print("Has 'sticker' or 'stickers':", has_sticker)
    except Exception as e:
        print("Error:", type(e).__name__, e)

    print("\n" + "=" * 60)
    print("2. sticker/list (limit=5)")
    print("=" * 60)
    try:
        data = post(base, "/sticker/list", {"limit": 5})
        text = json.dumps(data, ensure_ascii=False, indent=2)
        lines = text.splitlines()
        for line in lines[:80]:
            print(line)
        if len(lines) > 80:
            print(f"... ({len(lines) - 80} more lines)")
    except Exception as e:
        print("Error:", type(e).__name__, e)


if __name__ == "__main__":
    main()
