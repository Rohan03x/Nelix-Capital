from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class R2Config:
    endpoint_url: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    region: str = "auto"
    prefix: str = "nelix-learning"


def _first_env(*names: str) -> str:
    for name in names:
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _normalise_prefix(value: str) -> str:
    return str(value or "").strip().strip("/")


def r2_config() -> R2Config | None:
    access_key_id = _first_env("LEARNING_R2_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
    secret_access_key = _first_env("LEARNING_R2_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
    bucket = _first_env("LEARNING_R2_BUCKET", "R2_BUCKET", "LEARNING_SNAPSHOT_BUCKET") or "learning-state"
    endpoint_url = _first_env("LEARNING_R2_ENDPOINT_URL", "LEARNING_R2_ENDPOINT", "R2_ENDPOINT_URL", "R2_ENDPOINT")
    account_id = _first_env("LEARNING_R2_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID")
    if not endpoint_url and account_id:
        endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
    if not (endpoint_url and bucket and access_key_id and secret_access_key):
        return None
    return R2Config(
        endpoint_url=endpoint_url.rstrip("/"),
        bucket=bucket.strip(),
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        region=_first_env("LEARNING_R2_REGION", "R2_REGION") or "auto",
        prefix=_normalise_prefix(_first_env("LEARNING_R2_PREFIX", "R2_PREFIX") or "nelix-learning"),
    )


def r2_enabled() -> bool:
    return r2_config() is not None


def _truthy_env(name: str, default: bool = False) -> bool:
    value = str(os.environ.get(name) or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return max(int(str(os.environ.get(name) or "").strip() or default), 1)
    except (TypeError, ValueError):
        return int(default)


def cache_read_through_enabled() -> bool:
    return r2_enabled() and _truthy_env("LEARNING_R2_CACHE_READ_THROUGH", False)


def cache_write_through_enabled() -> bool:
    return r2_enabled() and _truthy_env("LEARNING_R2_CACHE_WRITE_THROUGH", False)


def object_key(key: str, config: R2Config | None = None) -> str:
    cfg = config or r2_config()
    raw_key = str(key or "").strip().replace("\\", "/").lstrip("/")
    if not cfg or not cfg.prefix:
        return raw_key
    return f"{cfg.prefix}/{raw_key}" if raw_key else cfg.prefix


def cache_object_key(filename: str) -> str:
    return f"cache/webapp/data/cache/{Path(filename).name}"


def snapshot_object_key(namespace: str) -> str:
    safe_namespace = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in namespace)
    return f"snapshots/{safe_namespace}.json"


def _payload_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _canonical_query(query: str) -> str:
    pairs = parse_qsl(query, keep_blank_values=True)
    encoded = [
        (quote(str(name), safe="-_.~"), quote(str(value), safe="-_.~"))
        for name, value in pairs
    ]
    return "&".join(f"{name}={value}" for name, value in sorted(encoded))


def _signing_key(secret_access_key: str, date_stamp: str, region: str) -> bytes:
    date_key = hmac.new(("AWS4" + secret_access_key).encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256).digest()
    region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _signed_headers(
    config: R2Config,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    *,
    body_hash: str | None = None,
) -> dict[str, str]:
    parsed = urlsplit(url)
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    body_hash = body_hash or _payload_hash(body)

    canonical_header_values = {
        str(name).lower(): " ".join(str(value).strip().split())
        for name, value in headers.items()
        if str(name).lower() != "authorization"
    }
    canonical_header_values["host"] = parsed.netloc
    canonical_header_values["x-amz-content-sha256"] = body_hash
    canonical_header_values["x-amz-date"] = amz_date

    signed_header_names = sorted(canonical_header_values)
    canonical_headers = "".join(f"{name}:{canonical_header_values[name]}\n" for name in signed_header_names)
    signed_headers = ";".join(signed_header_names)
    canonical_request = "\n".join(
        [
            method.upper(),
            quote(parsed.path or "/", safe="/~"),
            _canonical_query(parsed.query),
            canonical_headers,
            signed_headers,
            body_hash,
        ]
    )
    credential_scope = f"{date_stamp}/{config.region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(config.secret_access_key, date_stamp, config.region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    signed = dict(headers)
    signed["Host"] = parsed.netloc
    signed["X-Amz-Content-Sha256"] = body_hash
    signed["X-Amz-Date"] = amz_date
    signed["Authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={config.access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    return signed


def _object_url(config: R2Config, key: str) -> str:
    full_key = object_key(key, config)
    return f"{config.endpoint_url}/{quote(config.bucket, safe='')}/{quote(full_key, safe='/~')}"


def request_object(
    method: str,
    key: str,
    *,
    data: bytes | str | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
):
    config = r2_config()
    if config is None:
        return None
    import requests

    body = data.encode("utf-8") if isinstance(data, str) else (data or b"")
    request_headers = dict(headers or {})
    signed = _signed_headers(config, method, _object_url(config, key), request_headers, body)
    return requests.request(method.upper(), _object_url(config, key), headers=signed, data=body, timeout=timeout)


def get_object(key: str, *, timeout: int = 60) -> bytes | None:
    response = request_object("GET", key, timeout=timeout)
    if response is None or response.status_code == 404:
        return None
    if response.status_code != 200:
        raise RuntimeError(f"R2 object load failed for {key}: {response.status_code} {response.reason}")
    return bytes(response.content or b"")


def put_object(key: str, data: bytes | str, *, content_type: str = "application/octet-stream", timeout: int = 120) -> bool:
    response = request_object("PUT", key, data=data, headers={"Content-Type": content_type}, timeout=timeout)
    if response is None:
        return False
    if response.status_code in {200, 201, 204}:
        return True
    raise RuntimeError(f"R2 object save failed for {key}: {response.status_code} {response.reason}: {response.text[:300]}")


def _file_sha256(source_path: Path) -> str:
    digest = hashlib.sha256()
    with source_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def put_file_object(
    key: str,
    source: Path | str,
    *,
    content_type: str = "application/octet-stream",
    timeout: int | None = None,
) -> bool:
    config = r2_config()
    if config is None:
        return False
    import requests

    source_path = Path(source)
    request_headers = {
        "Content-Type": content_type,
        "Content-Length": str(source_path.stat().st_size),
    }
    url = _object_url(config, key)
    signed = _signed_headers(
        config,
        "PUT",
        url,
        request_headers,
        b"",
        body_hash=_file_sha256(source_path),
    )
    with source_path.open("rb") as handle:
        response = requests.request("PUT", url, headers=signed, data=handle, timeout=timeout or _env_int("LEARNING_R2_UPLOAD_TIMEOUT", 900))
    if response.status_code in {200, 201, 204}:
        return True
    raise RuntimeError(f"R2 file save failed for {key}: {response.status_code} {response.reason}: {response.text[:300]}")


def load_json_object(key: str) -> Any | None:
    raw = get_object(key)
    if raw is None:
        return None
    return json.loads(raw.decode("utf-8"))


def save_json_object(key: str, payload: Any) -> bool:
    encoded = json.dumps(payload, default=str).encode("utf-8")
    return put_object(key, encoded, content_type="application/json")


def download_file(key: str, destination: Path | str) -> bool:
    raw = get_object(key)
    if raw is None:
        return False
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return True


def upload_file(key: str, source: Path | str, *, content_type: str | None = None) -> bool:
    source_path = Path(source)
    guessed_type = content_type or mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
    return put_file_object(key, source_path, content_type=guessed_type)


def download_cache_file(filename: str, destination: Path | str) -> bool:
    if not cache_read_through_enabled():
        return False
    try:
        return download_file(cache_object_key(filename), destination)
    except Exception as exc:
        logger.debug("R2 cache read-through failed for %s: %s", filename, exc)
        return False


def upload_cache_file(filename: str, source: Path | str) -> bool:
    if not cache_write_through_enabled():
        return False
    try:
        return upload_file(cache_object_key(filename), source)
    except Exception as exc:
        logger.debug("R2 cache write-through failed for %s: %s", filename, exc)
        return False


def backend_summary() -> dict[str, Any]:
    config = r2_config()
    if config is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "bucket": config.bucket,
        "prefix": config.prefix,
        "endpoint_configured": bool(config.endpoint_url),
        "cache_read_through": cache_read_through_enabled(),
        "cache_write_through": cache_write_through_enabled(),
    }