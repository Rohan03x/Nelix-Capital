"""Prepare static assets for Vercel deployment."""

from __future__ import annotations

import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE_STATIC = ROOT / "webapp" / "static"
PUBLIC_STATIC = ROOT / "public" / "static"


def main() -> None:
    if not SOURCE_STATIC.exists():
        raise FileNotFoundError(f"Static asset directory not found: {SOURCE_STATIC}")

    if PUBLIC_STATIC.exists():
        shutil.rmtree(PUBLIC_STATIC)

    PUBLIC_STATIC.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE_STATIC, PUBLIC_STATIC)
    print(f"Copied {SOURCE_STATIC} -> {PUBLIC_STATIC}")


if __name__ == "__main__":
    main()