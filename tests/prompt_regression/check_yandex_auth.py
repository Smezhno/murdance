"""One-off check: can we authorize with YandexGPT API?

Run from project root: python -m tests.prompt_regression.check_yandex_auth
Uses .env (YANDEXGPT_API_KEY, YANDEXGPT_FOLDER_ID). Does not touch .env.
"""

import asyncio
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))


async def main() -> None:
    from app.ai.providers.yandexgpt import YandexGPTProvider

    p = YandexGPTProvider()
    if not p.api_key or not p.folder_id:
        print("FAIL: YANDEXGPT_API_KEY or YANDEXGPT_FOLDER_ID is empty (check .env)")
        sys.exit(1)
    print(f"Key length: {len(p.api_key)}, folder_id: {p.folder_id[:8]}...")
    try:
        r = await p.call([{"role": "user", "content": "Скажи одно слово: привет"}])
        print("OK: YandexGPT responded:", r.text[:80] if r.text else "(empty)")
    except Exception as e:
        print("FAIL:", e)
        print("\nTo test from terminal (replace KEY and FOLDER from .env):")
        print('  curl -s -X POST "https://llm.api.cloud.yandex.net/foundationModels/v1/completion" \\')
        print('    -H "Authorization: Api-Key $YANDEXGPT_API_KEY" \\')
        print('    -H "x-folder-id: $YANDEXGPT_FOLDER_ID" \\')
        print('    -H "Content-Type: application/json" \\')
        print('    -d \'{"modelUri":"gpt://$YANDEXGPT_FOLDER_ID/yandexgpt/latest","completionOptions":{"stream":false,"maxTokens":"10"},"messages":[{"role":"user","text":"Hi"}]}\'')
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
