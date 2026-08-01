"""
================================================================================
LIVE DEPLOYMENT SMOKE TEST — Railway
================================================================================
What this does:
    Self-contained (Python standard library only — no pip installs needed)
    end-to-end check against the live Railway deployment: logs in, runs a
    pricing calculation, and asserts the result falls within the expected
    range for entry_age=35, tier=1, whole_life, dependant=spouse,
    mortality_loading=-0.20, target_margin=0.15.

    Credentials are read from environment variables ONLY — never hardcoded
    here, never accepted as a command-line argument (both would end up in
    shell history or, worse, a committed file). Create an admin user first
    via scripts/create_admin.py if you haven't already (see README.md).

Usage (PowerShell):
    $env:ADMIN_EMAIL = "admin@stallion.com"
    $env:ADMIN_PASSWORD = "your-password"
    python scripts/test_live_deployment.py

Usage (bash):
    ADMIN_EMAIL=admin@stallion.com ADMIN_PASSWORD=your-password python scripts/test_live_deployment.py

Exit code: 0 on PASS, 1 on FAIL (or any error) — usable in CI.
================================================================================
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://web-production-efcac.up.railway.app"

PRICING_REQUEST = {
    "entry_age":         35,
    "tier":              1,
    "product":           "whole_life",
    "dependant":         "spouse",
    "mortality_loading": -0.20,
    "target_margin":     0.15,
}

# Known-correct benchmark range for the request above (see engine/pricing.py
# / tests/ for how the underlying premium solver is validated).
EXPECTED_MONTHLY_PREMIUM_RANGE = (16.00, 17.00)
EXPECTED_PROFIT_MARGIN_RANGE   = (0.14, 0.16)

REQUEST_TIMEOUT_SECONDS = 30


def _request(method: str, path: str, *, data: dict = None, headers: dict = None, form: bool = False):
    """
    Minimal HTTP JSON client using only urllib (stdlib) — no extra
    dependencies for this script to work. Returns (status_code, parsed_body)
    for both success and HTTP error responses, so callers can inspect the
    error body FastAPI sends back instead of just catching an exception.
    """
    url = BASE_URL + path
    headers = dict(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = json.dumps(data).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except urllib.error.URLError as e:
        return None, str(e.reason)


def main() -> int:
    email    = os.environ.get("ADMIN_EMAIL")
    password = os.environ.get("ADMIN_PASSWORD")
    if not email or not password:
        print("FAIL: set ADMIN_EMAIL and ADMIN_PASSWORD environment variables before running this script.")
        return 1

    # ── 1. Log in ────────────────────────────────────────────────────────
    print(f"Logging in as {email} at {BASE_URL} ...")
    status, body = _request(
        "POST", "/auth/login",
        data={"username": email, "password": password}, form=True,
    )
    if status != 200:
        print(f"FAIL: POST /auth/login returned HTTP {status}: {body}")
        return 1

    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        print(f"FAIL: login response had no access_token: {body}")
        return 1
    print("Login OK — token acquired.")

    # ── 2. Run pricing ──────────────────────────────────────────────────
    print(f"Calling POST /pricing with {PRICING_REQUEST} ...")
    status, body = _request(
        "POST", "/pricing",
        data=PRICING_REQUEST,
        headers={"Authorization": f"Bearer {token}"},
    )
    if status != 200:
        print(f"FAIL: POST /pricing returned HTTP {status}: {body}")
        return 1

    result = body.get("data", {}) if isinstance(body, dict) else {}
    monthly_premium = result.get("monthly_premium")
    profit_margin   = result.get("profit_margin")
    print(f"Received: monthly_premium={monthly_premium}  profit_margin={profit_margin}")

    # ── 3. Assert against the expected range ────────────────────────────
    failures = []
    if not isinstance(monthly_premium, (int, float)) or not (
        EXPECTED_MONTHLY_PREMIUM_RANGE[0] <= monthly_premium <= EXPECTED_MONTHLY_PREMIUM_RANGE[1]
    ):
        failures.append(
            f"monthly_premium {monthly_premium!r} not in expected range "
            f"[{EXPECTED_MONTHLY_PREMIUM_RANGE[0]:.2f}, {EXPECTED_MONTHLY_PREMIUM_RANGE[1]:.2f}]"
        )
    if not isinstance(profit_margin, (int, float)) or not (
        EXPECTED_PROFIT_MARGIN_RANGE[0] <= profit_margin <= EXPECTED_PROFIT_MARGIN_RANGE[1]
    ):
        failures.append(
            f"profit_margin {profit_margin!r} not in expected range "
            f"[{EXPECTED_PROFIT_MARGIN_RANGE[0]:.2f}, {EXPECTED_PROFIT_MARGIN_RANGE[1]:.2f}]"
        )

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"PASS: monthly_premium={monthly_premium:.2f} "
        f"(expected [{EXPECTED_MONTHLY_PREMIUM_RANGE[0]:.2f}, {EXPECTED_MONTHLY_PREMIUM_RANGE[1]:.2f}]), "
        f"profit_margin={profit_margin:.4f} "
        f"(expected [{EXPECTED_PROFIT_MARGIN_RANGE[0]:.2f}, {EXPECTED_PROFIT_MARGIN_RANGE[1]:.2f}])"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
