from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from auto_valuation.config import ROOT_DIR
from auto_valuation.learning.r2_store import backend_summary, save_json_object, upload_file


CACHE_SPECS: tuple[tuple[str, str], ...] = (
    ("webapp/data/cache", "cache/webapp/data/cache"),
)

DEFAULT_SPECS: tuple[tuple[str, str], ...] = (
    ("auto_valuation/learning/db", "brain/db"),
    ("auto_valuation/learning/ledger", "brain/ledger"),
)

SKIP_SUFFIXES = ("-wal", "-shm", ".lock", ".tmp")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return ()
    return (
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(SKIP_SUFFIXES)
    )


def _sqlite_backup(source: Path, temp_dir: Path) -> Path:
    backup_path = temp_dir / source.name
    try:
        src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            dst_conn = sqlite3.connect(backup_path)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        return backup_path
    except Exception:
        shutil.copy2(source, backup_path)
        return backup_path


def _upload_source_path(source: Path, temp_dir: Path) -> Path:
    if source.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        return _sqlite_backup(source, temp_dir)
    return source


def mirror_to_r2(
    *,
    specs: tuple[tuple[str, str], ...] = DEFAULT_SPECS,
    dry_run: bool = False,
    limit: int | None = None,
    include_hash: bool = False,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "generated_at": _utcnow_iso(),
        "backend": backend_summary(),
        "dry_run": bool(dry_run),
        "entries": [],
        "totals": {"files": 0, "bytes": 0, "uploaded": 0, "failed": 0},
    }
    max_files = max(int(limit or 0), 0)
    with tempfile.TemporaryDirectory(prefix="nelix-r2-mirror-") as tmp_name:
        temp_dir = Path(tmp_name)
        for local_rel, remote_prefix in specs:
            local_root = ROOT_DIR / local_rel
            for source in _iter_files(local_root):
                if max_files and int(manifest["totals"]["files"]) >= max_files:
                    break
                relative = source.relative_to(local_root).as_posix()
                object_name = f"{remote_prefix.strip('/')}/{relative}"
                upload_path = _upload_source_path(source, temp_dir)
                size = upload_path.stat().st_size
                entry: dict[str, Any] = {
                    "local_path": str(source.relative_to(ROOT_DIR)),
                    "object_key": object_name,
                    "bytes": size,
                    "uploaded": False,
                }
                if include_hash:
                    entry["sha256"] = _sha256(upload_path)
                if not dry_run:
                    try:
                        entry["uploaded"] = bool(upload_file(object_name, upload_path))
                    except Exception as exc:
                        entry["error"] = str(exc)
                manifest["entries"].append(entry)
                manifest["totals"]["files"] += 1
                manifest["totals"]["bytes"] += size
                if entry.get("uploaded"):
                    manifest["totals"]["uploaded"] += 1
                elif not dry_run:
                    manifest["totals"]["failed"] += 1
            if max_files and int(manifest["totals"]["files"]) >= max_files:
                break
    if not dry_run:
        save_json_object("manifests/full-brain-cache.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror local learning brain/cache files into Cloudflare R2.")
    parser.add_argument("--dry-run", action="store_true", help="Build the manifest without uploading objects.")
    parser.add_argument("--limit", type=int, default=0, help="Upload at most this many files; useful for smoke tests.")
    parser.add_argument("--hash", action="store_true", help="Include SHA-256 checksums in the manifest.")
    parser.add_argument("--include-cache", action="store_true", help="Also mirror raw webapp/data/cache market-data files.")
    parser.add_argument("--cache-only", action="store_true", help="Mirror only raw webapp/data/cache market-data files.")
    parser.add_argument("--brain-only", action="store_true", help="Mirror only learning DB and ledger files.")
    args = parser.parse_args()

    specs = DEFAULT_SPECS
    if args.cache_only:
        specs = CACHE_SPECS
    elif args.brain_only:
        specs = DEFAULT_SPECS
    elif args.include_cache:
        specs = DEFAULT_SPECS + CACHE_SPECS

    manifest = mirror_to_r2(
        specs=specs,
        dry_run=bool(args.dry_run),
        limit=args.limit or None,
        include_hash=bool(args.hash),
    )
    print(json.dumps({"backend": manifest["backend"], "totals": manifest["totals"], "dry_run": manifest["dry_run"]}, indent=2))
    return 0 if manifest["dry_run"] or int(manifest["totals"].get("failed") or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())