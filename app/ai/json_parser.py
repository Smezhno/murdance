"""3-step JSON extraction parser.

Per CONTRACT §11: Never crash on bad LLM output.
Steps: parse → extract code block → retry → fallback to None
"""

import json
import re
from typing import Any


def extract_json(text: str) -> dict[str, Any] | None:
    """Extract JSON from LLM response using 3-step process (CONTRACT §11).

    Step 1: Try standard JSON.parse()
    Step 2: Extract from markdown code block (```json ... ```)
    Step 3: Fallback to None (never crash)

    Args:
        text: LLM response text

    Returns:
        Parsed JSON dict or None if extraction fails
    """
    # Step 1: Try standard JSON parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Step 2: Extract from markdown code block
    # Pattern: ```json ... ``` or ``` ... ```
    code_block_patterns = [
        r"```json\s*\n(.*?)\n```",
        r"```\s*\n(.*?)\n```",
        r"```json\s*(.*?)\s*```",
        r"```\s*(.*?)\s*```",
    ]

    for pattern in code_block_patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            json_str = match.group(1).strip()
            try:
                return json.loads(json_str)
            except (json.JSONDecodeError, ValueError):
                continue

    # Step 3: Fallback to None (never crash)
    return None


def extract_json_with_retry(text: str, retry_text: str | None = None) -> dict[str, Any] | None:
    """Extract JSON with optional retry text.

    If retry_text is provided, try parsing it first.

    Args:
        text: Original LLM response text
        retry_text: Retry response text (from LLM asked to "respond ONLY in valid JSON")

    Returns:
        Parsed JSON dict or None if extraction fails
    """
    if retry_text:
        result = extract_json(retry_text)
        if result is not None:
            return result

    return extract_json(text)
