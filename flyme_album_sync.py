#!/usr/bin/env python3
"""
Sync a Flyme Photos album from a browser "Copy as cURL" request.

The script does not store a token. Save the copied cURL command to a local file
and pass it with --curl-file.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


API_BASE = "https://mzstorage.meizu.com"
KS = "*Photos$@w7c"
PRIVATE_KEY_B64 = (
    "MIICdgIBADANBgkqhkiG9w0BAQEFAASCAmAwggJcAgEAAoGBAJpODQgsoTzXkDDx9x1TZ8UZu70YxTHgt+mEVxho8b57p9h8WaELWzSp43DVuxl60ral2Ri4jieUlZoioy+f6zqK8ng8QgvDzDGOlEjPj0kV35oVHouZvY5bc9bhPsqVpVummQDOgGM6pf7YWAx9lasKK/TMPvzDtqMVwlXsZXLdAgMBAAECgYBzG5B7LZfmZERLTuVyOfrqPOUhDi5ko+duSuwR6I+V8nbmdvUBvw/9vFJPpREa09YGrLfDykE5Y40qW3Zym5CEgajLvZHTopVCBPpK/xLJjcw/tPnE5ky++ytXZ6QFAVQg41lGuC6qBqSdnB8JRsnN+XDXn/pUIqBlBX1pb43I4QJBAOvfd/cT9pBHcROcFjAQchQHrihA3tm0iQBkgCy+o6AYuaC+PWY1mCDGaXHrc2+F4/Tv4tEt1FnXZ2sZytxRWwUCQQCneMR4HGy9jKJ5jdejtjy4Y5E0TDhXJyjukk1VBMvK31RYJe+e6RkAK4UUa2g6E/Z3JRkXtyDxB6oviKTMti/5AkEAzLc3N4psBOz8hziBSVX8rMW9sdIbmHfIMD8Jv8v1142eDpUOVRdO4aNTATyJA9IA9yT8hvBvzUnWyG2qU22IwQJAJ0QTnK3deRvuRF3Tf5kM55bAxuhQFW8jE7zN0O9M8QYn+nr6keHJcNbDXyRHzcY8dXcHSR4w5RKM/pQlP7I/0QJAUsKSNIC9Bc1Szfghocg+G2PtmXoJkSftbm3fkRbIWkCGxH36XZmKcULMSs6OQZAmcv7Q5Gj6X/0W25rTN/G5UQ=="
)


def log(message: str) -> None:
    print(message, flush=True)


def parse_curl_file(path: Path) -> tuple[str, dict[str, str], dict[str, str]]:
    raw = path.read_text(encoding="utf-8")
    tokens = shlex.split(raw.replace("\\\n", " "))
    if not tokens or tokens[0] != "curl":
        raise SystemExit(f"{path} does not look like a curl command")

    url = ""
    headers: dict[str, str] = {}
    body = ""
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("http"):
            url = token
        elif token in ("-H", "--header") and i + 1 < len(tokens):
            key, _, value = tokens[i + 1].partition(":")
            if key and value:
                headers[key.strip().lower()] = value.strip()
            i += 1
        elif token in ("--data-raw", "--data", "--data-binary", "-d") and i + 1 < len(tokens):
            body = tokens[i + 1]
            i += 1
        i += 1

    if not url:
        raise SystemExit("Could not find request URL in curl file")
    if not body:
        raise SystemExit("Could not find --data-raw body in curl file")

    params = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))
    if "token" not in params:
        raise SystemExit("curl body does not contain token")
    if "dirId" not in params:
        raise SystemExit("curl body does not contain dirId")
    return url, headers, params


def form_body(params: dict[str, Any]) -> str:
    return "&".join(f"{key}={value}" for key, value in params.items() if value is not None)


def sign_params(params: dict[str, Any]) -> str:
    clean = {
        str(key): str(value)
        for key, value in params.items()
        if value is not None and key not in ("token", "sign")
    }
    clean["ks"] = KS
    joined = "&".join(f"{key}={clean[key]}" for key in sorted(clean))
    return hashlib.md5(joined.encode("utf-8")).hexdigest()


def signed_body(params: dict[str, Any], token: str | None = None) -> bytes:
    body_params = dict(params)
    body_params["cts"] = str(int(time.time() * 1000))
    body_params.pop("sign", None)
    signature = sign_params(body_params)
    if token is not None:
        body_params["token"] = token
    elif "token" not in body_params:
        raise SystemExit("token is required")
    return f"{form_body(body_params)}&sign={signature}".encode("utf-8")


def request_json(url: str, headers: dict[str, str], body: bytes, timeout: int = 60) -> dict[str, Any]:
    req_headers = {
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://photos.flyme.cn",
        "referer": "https://photos.flyme.cn/",
        "user-agent": headers.get(
            "user-agent",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        ),
    }
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_album_pages(
    headers: dict[str, str],
    base_params: dict[str, str],
    cache_dir: Path,
    max_pages: int | None,
) -> list[dict[str, Any]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    token = base_params["token"]
    limit = int(base_params.get("limit") or 34)
    offset = int(base_params.get("offset") or 0)
    pages: list[dict[str, Any]] = []
    total_count: int | None = None

    while True:
        page_params = {
            key: value
            for key, value in base_params.items()
            if key not in ("token", "sign", "cts", "offset")
        }
        page_params["offset"] = str(offset)
        page_params["limit"] = str(limit)
        body = signed_body(page_params, token=token)
        data = request_json(f"{API_BASE}/album/list", headers, body)
        if data.get("code") != 200:
            raise SystemExit(f"album/list failed at offset={offset}: {data}")

        page_path = cache_dir / f"album-list-offset-{offset}.json"
        page_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        value = data.get("value") or {}
        files = value.get("file") or []
        total_count = int(value.get("count") or total_count or 0)
        pages.extend(files)
        log(f"listed offset={offset}: {len(files)} files, total={len(pages)}/{total_count}")

        if value.get("end") == 1 or not files:
            break
        offset += limit
        if max_pages and len(pages) >= max_pages * limit:
            break
    return pages


def private_key_pem(cache_dir: Path) -> Path:
    key_path = cache_dir / "flyme-private-key.pem"
    wrapped = "\n".join(PRIVATE_KEY_B64[i : i + 64] for i in range(0, len(PRIVATE_KEY_B64), 64))
    key_path.write_text(
        f"-----BEGIN PRIVATE KEY-----\n{wrapped}\n-----END PRIVATE KEY-----\n",
        encoding="ascii",
    )
    key_path.chmod(0o600)
    return key_path


def openssl_decrypt_chunk(chunk: bytes, key_path: Path) -> bytes:
    commands = [
        ["openssl", "rsautl", "-decrypt", "-inkey", str(key_path)],
        ["openssl", "pkeyutl", "-decrypt", "-inkey", str(key_path), "-pkeyopt", "rsa_padding_mode:pkcs1"],
    ]
    last_error = ""
    for cmd in commands:
        proc = subprocess.run(cmd, input=chunk, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode == 0:
            return proc.stdout
        last_error = proc.stderr.decode("utf-8", errors="replace")
    raise RuntimeError(last_error.strip() or "openssl RSA decrypt failed")


def decrypt_long(cipher_text: str, cache_dir: Path) -> str:
    key_path = private_key_pem(cache_dir)
    compact = re.sub(r"\s+", "", cipher_text)
    chunks: list[bytes] = []
    if len(compact) % 172 == 0:
        for i in range(0, len(compact), 172):
            chunks.append(base64.b64decode(compact[i : i + 172]))
    else:
        decoded = base64.b64decode(compact)
        key_size = 128
        chunks = [decoded[i : i + key_size] for i in range(0, len(decoded), key_size)]

    plain = b"".join(openssl_decrypt_chunk(chunk, key_path) for chunk in chunks)
    return plain.decode("utf-8")


def get_oss_sig(headers: dict[str, str], token: str, cache_dir: Path) -> dict[str, Any]:
    sig_cache = cache_dir / "oss-sig.json"
    if sig_cache.exists():
        cached = json.loads(sig_cache.read_text(encoding="utf-8"))
        if int(cached.get("_cached_until", 0)) > int(time.time()) + 300:
            return cached

    body = signed_body({"type": "2"}, token=token)
    data = request_json(f"{API_BASE}/file/get_sig/v2", headers, body)
    if data.get("code") != 200:
        raise SystemExit(f"file/get_sig/v2 failed: {data}")
    value = json.loads(decrypt_long(str(data["value"]), cache_dir))
    value["_cached_until"] = int(time.time()) + 50 * 60
    sig_cache.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return value


def signed_oss_url(key: str, sig: dict[str, Any], attachment: bool = False) -> str:
    expires = int(time.time()) + 3600
    disposition = f"?response-content-disposition=attachment;filename={key}&security-token={sig['securityToken']}"
    security = f"?security-token={sig['securityToken']}"
    canonical = f"GET\n\n\n{expires}\n/{sig['bucket']}/{key}{disposition if attachment else security}"
    signature = base64.b64encode(
        hmac.new(str(sig["accessKeySecret"]).encode("utf-8"), canonical.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")
    query = {
        "OSSAccessKeyId": sig["accessKeyId"],
        "Expires": str(expires),
        "Signature": signature,
        "security-token": sig["securityToken"],
    }
    if attachment:
        query["response-content-disposition"] = f"attachment;filename={key}"
    return f"https://{sig['bucket']}.{sig['region']}.aliyuncs.com/{key}?{urllib.parse.urlencode(query)}"


def find_existing_file(out_dir: Path, item: dict[str, Any]) -> Path | None:
    target = out_dir / item["fileName"]
    if target.exists():
        return target
    md5 = str(item.get("md5") or "").split("_")[0]
    if md5:
        matches = list(out_dir.glob(f"*{md5}*"))
        if matches:
            return matches[0]
    return None


def download_file(url: str, target: Path, expected_size: int | None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    req = urllib.request.Request(url, headers={"user-agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as resp, part.open("wb") as fh:
        shutil.copyfileobj(resp, fh)
    if expected_size and part.stat().st_size != expected_size:
        raise RuntimeError(f"size mismatch for {target.name}: {part.stat().st_size} != {expected_size}")
    part.replace(target)


def rename_to_api_name(path: Path, item: dict[str, Any]) -> Path:
    target = path.parent / item["fileName"]
    if path == target:
        return path
    if target.exists():
        return target
    path.rename(target)
    return target


def local_datetime_from_ms(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000).astimezone()


def write_metadata(path: Path, item: dict[str, Any]) -> None:
    shoot_time = int(item.get("shootTime") or item.get("createTime") or 0)
    if shoot_time <= 0:
        return
    local_dt = local_datetime_from_ms(shoot_time)
    exif_dt = local_dt.strftime("%Y:%m:%d %H:%M:%S%z")
    touch_dt = local_dt.strftime("%Y%m%d%H%M.%S")
    setfile_dt = local_dt.strftime("%m/%d/%Y %H:%M:%S")
    ext = path.suffix.lower()

    if ext in {".jpg", ".jpeg", ".heic", ".tif", ".tiff", ".png", ".gif", ".webp"} and shutil.which("exiftool"):
        subprocess.run(
            [
                "exiftool",
                "-overwrite_original",
                "-P",
                f"-DateTimeOriginal={exif_dt}",
                f"-CreateDate={exif_dt}",
                f"-ModifyDate={exif_dt}",
                f"-XMP:CreateDate={exif_dt}",
                f"-XMP:DateTimeOriginal={exif_dt}",
                str(path),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if shutil.which("SetFile"):
        subprocess.run(["SetFile", "-d", setfile_dt, str(path)], check=False)
        subprocess.run(["SetFile", "-m", setfile_dt, str(path)], check=False)
    subprocess.run(["touch", "-mt", touch_dt, str(path)], check=False)


def strict_residuals(out_dir: Path) -> list[Path]:
    pattern = re.compile(r"^[0-9]+_[0-9a-f]{32}.*", re.IGNORECASE)
    return sorted(path for path in out_dir.iterdir() if path.is_file() and pattern.match(path.name))


def sync_files(
    items: list[dict[str, Any]],
    out_dir: Path,
    headers: dict[str, str],
    token: str,
    cache_dir: Path,
    download: bool,
    metadata: bool,
    dry_run: bool,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    sig: dict[str, Any] | None = None
    done = 0
    skipped = 0
    downloaded = 0

    for index, item in enumerate(items, start=1):
        name = item["fileName"]
        path = find_existing_file(out_dir, item)

        if path is None and download:
            if sig is None:
                sig = get_oss_sig(headers, token, cache_dir)
            path = out_dir / name
            if dry_run:
                log(f"[dry-run] download {index}/{len(items)} {name}")
            else:
                url = signed_oss_url(item["url"], sig, attachment=False)
                download_file(url, path, int(item.get("size") or 0) or None)
                downloaded += 1
                log(f"downloaded {index}/{len(items)} {name}")

        if path is None:
            skipped += 1
            log(f"missing {index}/{len(items)} {name}")
            continue

        if not dry_run:
            path = rename_to_api_name(path, item)
            if metadata:
                write_metadata(path, item)
        done += 1

    log(f"done={done}, downloaded={downloaded}, missing={skipped}")
    if out_dir.exists():
        residuals = strict_residuals(out_dir)
        if residuals:
            log("strict residual download-style names:")
            for path in residuals[:20]:
                log(f"  {path}")
            if len(residuals) > 20:
                log(f"  ... {len(residuals) - 20} more")
        else:
            log("strict residual check: clean")


def main() -> None:
    parser = argparse.ArgumentParser(description="Flyme album downloader and metadata syncer")
    parser.add_argument("--curl-file", required=True, type=Path, help="file containing Copy as cURL for album/list")
    parser.add_argument("--out", required=True, type=Path, help="output/download directory")
    parser.add_argument("--cache-dir", type=Path, help="cache directory, default: OUT/.flyme-sync")
    parser.add_argument("--download", action="store_true", help="download missing files")
    parser.add_argument("--metadata", action="store_true", default=True, help="write photo metadata and file times")
    parser.add_argument("--no-metadata", dest="metadata", action="store_false", help="do not write metadata")
    parser.add_argument("--dry-run", action="store_true", help="list planned operations only")
    parser.add_argument("--max-pages", type=int, help="debug limit for album/list pagination")
    args = parser.parse_args()

    _, headers, params = parse_curl_file(args.curl_file)
    cache_dir = args.cache_dir or (args.out / ".flyme-sync")
    items = fetch_album_pages(headers, params, cache_dir / "pages", args.max_pages)
    log(f"album files listed: {len(items)}")
    sync_files(
        items=items,
        out_dir=args.out,
        headers=headers,
        token=params["token"],
        cache_dir=cache_dir,
        download=args.download,
        metadata=args.metadata,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
