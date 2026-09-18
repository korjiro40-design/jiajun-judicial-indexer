#!/usr/bin/env python3
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import requests

WORKER_URL = os.getenv(
    "WORKER_URL",
    "https://dkqwnkplpgiauqknwbci.supabase.co/functions/v1/judicial-github-worker",
)
OIDC_AUDIENCE = "jiajun-judicial-indexer"
DOWNLOAD_URL = "https://opendata.judicial.gov.tw/api/FilesetLists/{fileset_id}/file"
MAX_MONTHS = int(os.getenv("MAX_MONTHS", "8"))
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", str(5 * 60 * 60)))
MAX_RECORDS_PER_CALL = 25
MAX_JSON_BYTES_PER_CALL = 4_000_000

session = requests.Session()
session.headers.update({"User-Agent": "jiajun-judicial-indexer/1.0"})


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def get_oidc_token():
    req_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    req_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not req_url or not req_token:
        raise RuntimeError("GitHub OIDC environment is unavailable; id-token: write is required")
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
    # GitHub OIDC tokens are short-lived; obtain a fresh one for each privileged call.
    token = get_oidc_token()
    body = {"action": action, **payload}
    r = session.post(
        WORKER_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        timeout=180,
    )
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"Worker HTTP {r.status_code}: {r.text[:1000]}")
    if not r.ok or not data.get("ok"):
        raise RuntimeError(data.get("error") or f"Worker HTTP {r.status_code}")
    return data


def get_member_token():
    return api("member_token")["token"]


def download_archive(batch, member_token, dest):
    url = DOWNLOAD_URL.format(fileset_id=batch["fileset_id"])
    log(f"Downloading {batch['year_month']} from fileSetId={batch['fileset_id']} ...")
    with session.get(
        url,
        headers={"Authorization": f"Bearer {member_token}"},
        stream=True,
        timeout=(30, 1800),
        allow_redirects=True,
    ) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    size = os.path.getsize(dest)
    if size < 32:
        raise RuntimeError(f"Downloaded archive is unexpectedly small: {size} bytes")
    log(f"Downloaded {size:,} bytes")
    return size


def run_extract(cmd):
    log("Extract: " + " ".join(cmd))
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"extract failed ({p.returncode}): {p.stdout[-4000:]}")


def extract_archive(archive, out_dir, fmt):
    fmt = (fmt or "").upper()
    os.makedirs(out_dir, exist_ok=True)
    errors = []
    if fmt == "RAR" or archive.lower().endswith(".rar"):
        if shutil.which("unar"):
            try:
                run_extract(["unar", "-quiet", "-force-overwrite", "-output-directory", out_dir, archive])
                return
            except Exception as e:
                errors.append(str(e))
        if shutil.which("7z"):
            try:
                run_extract(["7z", "x", "-y", f"-o{out_dir}", archive])
                return
            except Exception as e:
                errors.append(str(e))
    elif fmt in ("7Z", "ZIP") and shutil.which("7z"):
        run_extract(["7z", "x", "-y", f"-o{out_dir}", archive])
        return
    raise RuntimeError("No extractor succeeded: " + " | ".join(errors or [fmt]))


def decode_text(path):
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def looks_like_record(obj):
    if not isinstance(obj, dict):
        return False
    keys = {str(k).upper() for k in obj.keys()}
    return bool(keys & {"JID", "ID"}) and bool(keys & {"JFULL", "JFULLX", "JFULLCONTENT", "CONTENT", "全文"})


def iter_json_records(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from iter_json_records(x)
        return
    if not isinstance(obj, dict):
        return
    if looks_like_record(obj):
        yield obj
        return
    # Common wrappers first, then generic nested traversal.
    for key in ("data", "records", "items", "result", "results"):
        if key in obj:
            yield from iter_json_records(obj[key])
            return
    for v in obj.values():
        if isinstance(v, (dict, list)):
            yield from iter_json_records(v)


def iter_records(path):
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        text = decode_text(path)
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            # Some data sets are newline-delimited JSON despite .json extension.
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                yield from iter_json_records(obj)
            return
        yield from iter_json_records(obj)
        return
    if suffix == ".csv":
        text = decode_text(path)
        csv.field_size_limit(sys.maxsize)
        for row in csv.DictReader(text.splitlines()):
            if row:
                yield row
        return
    if suffix in (".txt", ".jsonl", ".ndjson"):
        text = decode_text(path)
        for line in text.splitlines():
            line = line.strip().lstrip("\ufeff")
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            yield from iter_json_records(obj)


def send_buffer(buf):
    if not buf:
        return 0, 0, 0
    r = api("ingest", records=buf)
    return int(r.get("docs", 0)), int(r.get("chunks", 0)), int(r.get("skipped", 0))


def ingest_directory(out_dir):
    files = [p for p in Path(out_dir).rglob("*") if p.is_file()]
    recognized = [p for p in files if p.suffix.lower() in (".json", ".csv", ".txt", ".jsonl", ".ndjson")]
    if not recognized:
        raise RuntimeError(f"No recognizable JSON/CSV/TXT files after extraction ({len(files)} files total)")

    total_docs = total_chunks = total_skipped = total_seen = 0
    buf = []
    buf_bytes = 2
    for path in recognized:
        log(f"Parsing {path.relative_to(out_dir)}")
        for record in iter_records(path):
            total_seen += 1
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if buf and (len(buf) >= MAX_RECORDS_PER_CALL or buf_bytes + len(encoded) > MAX_JSON_BYTES_PER_CALL):
                d, c, s = send_buffer(buf)
                total_docs += d; total_chunks += c; total_skipped += s
                log(f"Uploaded: docs={total_docs:,}, chunks={total_chunks:,}, seen={total_seen:,}")
                buf = []
                buf_bytes = 2
            buf.append(record)
            buf_bytes += len(encoded) + 1
    if buf:
        d, c, s = send_buffer(buf)
        total_docs += d; total_chunks += c; total_skipped += s
    if total_seen == 0:
        raise RuntimeError("Recognized files contained no judgment records")
    return {
        "docs": total_docs,
        "chunks": total_chunks,
        "skipped": total_skipped,
        "seen": total_seen,
        "files": len(recognized),
    }


def process_one(member_token):
    claim = api("claim")
    if claim.get("idle"):
        return False
    batch = claim["batch"]
    batch_id = batch["id"]
    ym = batch["year_month"]
    fmt = (batch.get("format") or "RAR").upper()
    log(f"Claimed {ym} (batch={batch_id}, format={fmt})")

    work = tempfile.mkdtemp(prefix=f"judicial-{ym}-")
    archive = os.path.join(work, f"archive.{fmt.lower()}")
    out_dir = os.path.join(work, "out")
    try:
        size = download_archive(batch, member_token, archive)
        api("mark_importing", batch_id=batch_id, bytes=size)
        extract_archive(archive, out_dir, fmt)
        result = ingest_directory(out_dir)
        api(
            "complete",
            batch_id=batch_id,
            docs=result["docs"],
            chunks=result["chunks"],
            bytes=size,
            meta={"files": result["files"], "records_seen": result["seen"], "skipped": result["skipped"], "worker": "github-actions"},
        )
        log(f"DONE {ym}: docs={result['docs']:,}, chunks={result['chunks']:,}, records={result['seen']:,}")
        return True
    except Exception as e:
        log(f"FAILED {ym}: {e}")
        try:
            api("fail", batch_id=batch_id, error=str(e))
        except Exception as report_error:
            log(f"Could not report failure: {report_error}")
        return True
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    started = time.time()
    log(f"Worker starting; max_months={MAX_MONTHS}, max_runtime={MAX_RUNTIME_SECONDS}s")
    # One Judicial Yuan member token is valid long enough for a multi-month run.
    member_token = get_member_token()
    completed_attempts = 0
    while completed_attempts < MAX_MONTHS and (time.time() - started) < MAX_RUNTIME_SECONDS:
        had_work = process_one(member_token)
        if not had_work:
            log("No pending batches. Import queue is empty.")
            break
        completed_attempts += 1
    try:
        status = api("status").get("stats", {})
        log("Final status: " + json.dumps(status, ensure_ascii=False))
    except Exception as e:
        log(f"Status check failed: {e}")
    log(f"Worker finished after {completed_attempts} month attempt(s)")


if __name__ == "__main__":
    main()
