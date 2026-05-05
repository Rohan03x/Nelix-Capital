from __future__ import annotations

import importlib.util
from pathlib import Path


_IMPL_PATH = Path(__file__).with_name("ticker-search") / "index.py"
_SPEC = importlib.util.spec_from_file_location("ticker_search_impl", _IMPL_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("Could not load ticker-search implementation")

_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

search_tickers_payload = _MODULE.search_tickers_payload
_cache_dir = _MODULE._cache_dir
_load_search_shard = _MODULE._load_search_shard


class handler(_MODULE.handler):
    pass