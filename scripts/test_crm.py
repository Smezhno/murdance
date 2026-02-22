"""Standalone script to test Impulse CRM authentication.

Tests three auth formats against the Impulse CRM public API:
  Format A: base64(f"{api_key}:")  — key as username, empty password (standard Basic)
  Format B: base64(f":{api_key}")  — empty username, key as password (standard Basic)
  Format C: raw key                — "Basic {api_key}" without base64 (Impulse-specific)

Usage: python scripts/test_crm.py
"""

import base64
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Load .env from project root
dotenv_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path)

CRM_TENANT = os.getenv("CRM_TENANT", "")
CRM_API_KEY = os.getenv("CRM_API_KEY", "")

if not CRM_TENANT or not CRM_API_KEY:
    print("ERROR: CRM_TENANT and CRM_API_KEY must be set in .env")
    sys.exit(1)

# Normalize tenant: accept both "myclub" and full URLs like "https://myclub.impulsecrm.ru/"
if "impulsecrm.ru" in CRM_TENANT:
    tenant_clean = CRM_TENANT.rstrip("/").split("//")[-1].split(".")[0]
else:
    tenant_clean = CRM_TENANT.strip()

URL = f"https://{tenant_clean}.impulsecrm.ru/api/public/group/list"
BODY = {"limit": 5}

print(f"Target URL: {URL}")
print(f"Tenant:     {tenant_clean} (raw: {CRM_TENANT.strip()})")
print(f"API key:    {CRM_API_KEY[:6]}{'*' * (len(CRM_API_KEY) - 6)}")
print()


def make_request(label: str, auth_header: str) -> tuple[bool, int, str, str]:
    """Make POST request with the given Authorization header value.

    Args:
        label:       Human-readable format name for display.
        auth_header: Full value of the Authorization header (e.g. "Basic xxx").

    Returns:
        Tuple of (success, status_code, content_type, body_preview).
    """
    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(URL, json=BODY, headers=headers)
        body = response.text[:500]
        content_type = response.headers.get("content-type", "")
        # True success: HTTP 2xx AND JSON body (not an HTML login page)
        is_json = (
            "application/json" in content_type
            or body.lstrip().startswith("{")
            or body.lstrip().startswith("[")
        )
        success = response.status_code < 400 and is_json
        return success, response.status_code, content_type, body
    except Exception as e:
        return False, 0, "", f"Connection error: {e}"


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


formats = [
    (
        "Format A — base64(key:)  [key as username]",
        f"Basic {b64(f'{CRM_API_KEY}:')}",
    ),
    (
        "Format B — base64(:key)  [key as password]",
        f"Basic {b64(f':{CRM_API_KEY}')}",
    ),
    (
        "Format C — raw key       [Impulse-specific, no base64]",
        f"Basic {CRM_API_KEY}",
    ),
]

results = []
for label, auth_header in formats:
    print(f"Testing {label}...")
    success, status, content_type, body = make_request(label, auth_header)
    results.append((label, success, status, content_type, body))
    icon = "✓" if success else "✗"
    print(f"  {icon} Status:       {status}")
    print(f"  Content-Type: {content_type}")
    print(f"  Response:     {body}")
    print()

# Summary
working = [(label, status) for label, success, status, _, _ in results if success]
n = len(formats)
print("=" * 60)
if working:
    for label, status in working:
        print(f"✅ WORKS: {label} (HTTP {status})")
else:
    print(f"❌ ALL {n} FORMATS FAILED — server returned HTML instead of JSON")
    print("   (HTTP 200 with HTML = login page redirect, not a valid API response)")
    print()
    print("Possible causes:")
    print("  1. API key is incorrect or expired")
    print("  2. Tenant subdomain is wrong")
    print("  3. API requires a different auth scheme (Bearer token, cookie, etc.)")
    print("  4. Endpoint path is wrong — check Impulse CRM docs for your version")
    print()
    print("Raw responses:")
    for label, _, status, ctype, body in results:
        print(f"  [{label}]")
        print(f"    HTTP {status}, Content-Type: {ctype}")
        print(f"    Body: {body[:200]}")
        print()
    sys.exit(1)
