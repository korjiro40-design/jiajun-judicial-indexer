#!/usr/bin/env python3
import json
import os

import import_judicial as base

CLAIM_URL = os.getenv(
    "CLAIM_URL",
    "https://dkqwnkplpgiauqknwbci.supabase.co/functions/v1/judicial-github-claim",
)

_original_api = base.api


def api(action, **payload):
    if action != "claim":
        return _original_api(action, **payload)

    token = base.get_oidc_token()
    body = {"action": "claim", **payload}
    r = base.session.post(
        CLAIM_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        timeout=180,
    )
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"Claim worker HTTP {r.status_code}: {r.text[:1000]}")
    if not r.ok or not data.get("ok"):
        raise RuntimeError(data.get("error") or f"Claim worker HTTP {r.status_code}")
    return data


base.api = api

if __name__ == "__main__":
    base.main()
