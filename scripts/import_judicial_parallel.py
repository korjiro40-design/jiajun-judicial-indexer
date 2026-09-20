#!/usr/bin/env python3
import json
import os
import time

import requests

import import_judicial as base

CLAIM_URL = os.getenv(
    "CLAIM_URL",
    "https://dkqwnkplpgiauqknwbci.supabase.co/functions/v1/judicial-github-claim",
)

_original_api = base.api


def api(action, **payload):
    if action != "claim":
        return _original_api(action, **payload)

    body = {"action": "claim", **payload}
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_error = None
    for attempt in range(1, base.API_MAX_ATTEMPTS + 1):
        try:
            token = base.get_oidc_token()
            r = base.session.post(
                CLAIM_URL,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                data=encoded,
                timeout=180,
            )
            try:
                data = r.json()
            except Exception:
                data = {}
            if r.ok and data.get("ok"):
                return data
            message = data.get("error") or f"Claim worker HTTP {r.status_code}: {r.text[:1000]}"
            last_error = RuntimeError(message)
            retryable = r.status_code in base.RETRYABLE_STATUS or any(
                code in message for code in ("57014", "53100", "PGRST002")
            )
            if not retryable:
                raise last_error
        except requests.RequestException as exc:
            last_error = exc
        if attempt < base.API_MAX_ATTEMPTS:
            delay = min(120, 5 * (3 ** (attempt - 1)))
            base.log(f"claim temporarily unavailable; retry {attempt}/{base.API_MAX_ATTEMPTS} in {delay}s: {last_error}")
            time.sleep(delay)
    raise RuntimeError(f"claim failed after {base.API_MAX_ATTEMPTS} attempts: {last_error}")


base.api = api

if __name__ == "__main__":
    base.main()
