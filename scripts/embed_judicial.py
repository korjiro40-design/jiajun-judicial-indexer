#!/usr/bin/env python3
import json
import os
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from sentence_transformers import SentenceTransformer

WORKER_URL = os.getenv(
    "EMBED_WORKER_URL",
    "https://dkqwnkplpgiauqknwbci.supabase.co/functions/v1/judicial-embedding-worker",
)
OIDC_AUDIENCE = "jiajun-judicial-indexer"
MODEL_NAME = "BAAI/bge-small-zh-v1.5"
CLAIM_BATCH = int(os.getenv("CLAIM_BATCH", "128"))
ENCODE_BATCH = int(os.getenv("ENCODE_BATCH", "32"))
MAX_CHUNKS = int(os.getenv("MAX_CHUNKS", "5000"))
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", str(4 * 60 * 60)))
API_MAX_ATTEMPTS = int(os.getenv("API_MAX_ATTEMPTS", "5"))
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

session = requests.Session()
session.headers.update({"User-Agent": "jiajun-judicial-embedder/1.0"})


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def get_oidc_token():
    req_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    req_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not req_url or not req_token:
        raise RuntimeError("GitHub OIDC environment unavailable")
    parts = urlsplit(req_url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q["audience"] = OIDC_AUDIENCE
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))
    r = session.get(url, headers={"Authorization": f"Bearer {req_token}"}, timeout=30)
    r.raise_for_status()
    token = r.json().get("value")
    if not token:
        raise RuntimeError("GitHub OIDC endpoint returned no token")
    return token


def api(action, **payload):
    body = json.dumps({"action": action, **payload}, ensure_ascii=False).encode("utf-8")
    last_error = None
    for attempt in range(1, API_MAX_ATTEMPTS + 1):
        try:
            token = get_oidc_token()
            r = session.post(
                WORKER_URL,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                data=body,
                timeout=180,
            )
            try:
                data = r.json()
            except Exception:
                data = {}
            if r.ok and data.get("ok"):
                return data
            message = data.get("error") or f"Worker HTTP {r.status_code}: {r.text[:1000]}"
            last_error = RuntimeError(message)
            retryable = r.status_code in RETRYABLE_STATUS or any(
                code in message for code in ("57014", "53100", "PGRST002")
            )
            if not retryable:
                raise last_error
        except requests.RequestException as exc:
            last_error = exc
        if attempt < API_MAX_ATTEMPTS:
            delay = min(120, 5 * (3 ** (attempt - 1)))
            log(f"{action} temporarily unavailable; retry {attempt}/{API_MAX_ATTEMPTS} in {delay}s: {last_error}")
            time.sleep(delay)
    raise RuntimeError(f"{action} failed after {API_MAX_ATTEMPTS} attempts: {last_error}")


def main():
    started = time.time()
    token = str(uuid.uuid4())
    processed = 0
    log(f"Loading {MODEL_NAME} on CPU")
    model = SentenceTransformer(MODEL_NAME, device="cpu")
    model.max_seq_length = 512
    log("Model ready")

    try:
        while processed < MAX_CHUNKS and (time.time() - started) < MAX_RUNTIME_SECONDS:
            claim_size = min(CLAIM_BATCH, MAX_CHUNKS - processed)
            data = api("claim", token=token, limit=claim_size)
            rows = data.get("rows") or []
            if not rows:
                log("No unembedded chunks currently available")
                break

            ids = [int(x["chunk_id"]) for x in rows]
            texts = [str(x.get("content") or "") for x in rows]
            log(f"Encoding {len(texts)} chunks; total before batch={processed}")
            vectors = model.encode(
                texts,
                batch_size=ENCODE_BATCH,
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            payload = [
                {"id": chunk_id, "embedding": vec.astype(float).tolist()}
                for chunk_id, vec in zip(ids, vectors)
            ]
            result = api("save", token=token, rows=payload)
            saved = int(result.get("saved") or 0)
            processed += saved
            log(f"Saved {saved} embeddings; total={processed}")
            if saved == 0:
                raise RuntimeError("embedding save returned zero rows")
    finally:
        try:
            released = api("release", token=token).get("released", 0)
            if released:
                log(f"Released {released} unfinished claims")
        except Exception as e:
            log(f"Claim release warning: {e}")

    log(f"Finished; embedded {processed} chunks in {int(time.time()-started)}s")


if __name__ == "__main__":
    main()
