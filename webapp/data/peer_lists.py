"""
webapp/data/peer_lists.py
─────────────────────────
Industry peer-group definitions and live metric fetching via EODHD.

Functions:
  get_peers_for_ticker(ticker, sector, industry) → list of tickers
  get_segment_peers(ticker)                       → {segment: [tickers]}
  fetch_peer_metrics(tickers, target_ticker)      → (peers_list, peer_median)
"""

from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any

from auto_valuation.learning.industry_taxonomy import industry_similarity, related_industries, resolve_industry_taxonomy

logger = logging.getLogger(__name__)

# Minimum industry-similarity score a displayed peer must meet.
# Learning bonuses may only reorder peers that have already cleared this gate.
_PEER_MIN_INDUSTRY_FIT: float = 0.45

# Max number of related-industry (not same-canonical) names allowed in the
# displayed basket. Same-canonical names are not capped.
_PEER_MAX_RELATED_FALLBACK: int = 3

# Cross-listing exclusion map: when searching peers for key ticker, always
# exclude these tickers because they are the SAME company on a different exchange.
# Format: "SUBJECT_TICKER" → {cross-listed variants to exclude}
_CROSS_LISTED_EXCLUSIONS: dict[str, set[str]] = {
    # Signify N.V.: PHPPY / PHPPY.US (OTC ADR) = LIGHT.AS (Euronext Amsterdam)
    "PHPPY.US": {"LIGHT.AS", "LIGHT"},
    "PHPPY":    {"LIGHT.AS", "LIGHT"},
    "LIGHT.AS": {"PHPPY.US", "PHPPY"},
    # Unilever: UL (OTC ADR) = ULVR.L (LSE) = UNA.AS (Euronext)
    "UL":       {"ULVR.L", "UNA.AS"},
    "ULVR.L":   {"UL", "UNA.AS"},
    "UNA.AS":   {"UL", "ULVR.L"},
    # Shell: SHEL = SHELL.AS
    "SHEL":     {"SHELL.AS", "RDSA.L", "RDSB.L"},
    "SHELL.AS": {"SHEL"},
    # BP
    "BP":       {"BP.L"},
    "BP.L":     {"BP"},
    # ASML
    "ASML":     {"ASML.AS"},
    "ASML.AS":  {"ASML"},
    # ABB: ABBNY (OTC) = ABBN.SW (SIX)
    "ABBNY":    {"ABBN.SW"},
    "ABBN.SW":  {"ABBNY"},
}


# ─── Peer group definitions ────────────────────────────────────────────────────

# Companies with distinct business segments get a custom basket per segment
MULTI_SEGMENT_PEERS: dict[str, dict[str, list[str]]] = {
    "AMZN": {
        "E-Commerce / Retail":    ["WMT", "COST", "BABA", "MELI", "EBAY", "SHOP", "JD", "TGT"],
        "Cloud (AWS)":            ["MSFT", "GOOGL", "ORCL", "IBM", "CRM"],
        "Digital Advertising":    ["GOOGL", "META", "TTD"],
        "Streaming (Prime)":      ["NFLX", "DIS", "PARA", "WBD"],
    },
    "GOOGL": {
        "Search & Advertising":   ["META", "MSFT", "TTD", "AMZN"],
        "Cloud (GCP)":            ["MSFT", "AMZN", "ORCL", "IBM"],
        "Hardware / Devices":     ["AAPL", "MSFT"],
    },
    "GOOG": {
        "Search & Advertising":   ["META", "MSFT", "TTD", "AMZN"],
        "Cloud (GCP)":            ["MSFT", "AMZN", "ORCL", "IBM"],
    },
    "MSFT": {
        "Office / Productivity":  ["GOOGL", "AAPL", "CRM", "NOW"],
        "Cloud (Azure)":          ["AMZN", "GOOGL", "ORCL", "IBM"],
        "Gaming":                 ["SONY", "NTDOY", "EA", "TTWO"],
    },
    "META": {
        "Social Media":           ["SNAP", "PINS", "RDDT", "GOOGL"],
        "Digital Advertising":    ["GOOGL", "TTD", "AMZN", "MSFT"],
    },
    "AAPL": {
        "Consumer Hardware":      ["MSFT", "GOOGL", "SONY", "HPQ"],
        "App Store / Services":   ["GOOGL", "MSFT", "SPOT", "NFLX"],
    },
    "TSLA": {
        "EVs / Autos":            ["NIO", "RIVN", "GM", "F", "TM", "STLA"],
        "Energy / Storage":       ["ENPH", "SEDG", "NEE"],
        "Autonomy / Software":    ["UBER", "GOOGL", "AMZN"],
    },
}

# Industry-level peer lists (canonical industry string → peer tickers)
INDUSTRY_PEER_MAP: dict[str, list[str]] = {
    # Consumer Cyclical
    "Internet Retail":            ["WMT", "COST", "BABA", "MELI", "EBAY", "SHOP", "JD", "TGT", "AMZN"],
    "Auto Manufacturers":         ["TSLA", "GM", "F", "TM", "HMC", "STLA", "RIVN", "NIO"],
    "Specialty Retail":           ["HD", "LOW", "TJX", "ROST", "BBY", "ULTA", "FIVE"],
    "Luxury Goods":               ["MC.PA", "RMS.PA", "KER.PA", "CFR.SW", "MONC.MI", "BRBY.L", "1913.HK", "RACE"],
    "Restaurants":                ["MCD", "SBUX", "CMG", "DRI", "YUM", "QSR", "WING"],
    "Hotels & Motels":            ["MAR", "HLT", "H", "WH", "IHG", "BKNG", "EXPE"],
    "Apparel Retail":             ["NKE", "LULU", "VFC", "PVH", "RL", "HBI", "UA"],
    "Footwear & Accessories":     ["NKE", "ADDYY", "LULU", "UA", "DECK", "SKX", "ONON"],
    "Leisure":                    ["DIS", "NFLX", "CMCSA", "WBD", "PARA"],
    "Home Improvement Retail":    ["HD", "LOW", "TSCO", "ORLY", "AZO"],
    # Technology
    "Software—Application":       ["MSFT", "CRM", "ORCL", "SAP", "NOW", "ADBE", "WDAY", "HUBS"],
    "Software—Infrastructure":    ["MSFT", "GOOGL", "ORCL", "IBM", "CSCO", "PANW", "FTNT"],
    "Semiconductors":             ["NVDA", "AMD", "INTC", "QCOM", "AVGO", "TSM", "ASML", "AMAT", "KLAC"],
    # Note: MSFT/GOOGL/META removed – they are Software/Internet, not Consumer Electronics.
    "Consumer Electronics":       ["AAPL", "SONY", "HPQ", "DELL", "1810.HK", "NTDOY"],
    # Electrical Equipment & Parts: lighting, switchgear, power management (canonical matches EODHD/yfinance)
    # LIGHT.AS (Signify NV Euronext) intentionally excluded — it is a cross-listing of PHPPY.US
    # Both keys maintained: "Electrical Equipment" (yfinance legacy) and "Electrical Equipment & Parts" (EODHD canonical)
    "Electrical Equipment":            ["AYI", "HUBB", "ETN", "LR.PA", "ABBN.SW", "EMR", "WOLF", "LYTS", "AMSAG.SW", "ZAG.VI"],
    "Electrical Equipment & Parts":    ["AYI", "HUBB", "ETN", "LR.PA", "ABBN.SW", "EMR", "WOLF", "LYTS", "AMSAG.SW", "ZAG.VI"],
    "Staffing & Employment Services": ["MAN", "ADEN.SW", "RAND.AS", "RHI", "KFY", "HSII"],
    "Electronic Components (Original)":  ["HON", "TE", "APTV", "FLEX", "JBL"],  # kept with rename for legacy
    "Internet Content & Information": ["META", "GOOGL", "SNAP", "PINS", "RDDT", "TWTR"],
    "Electronic Components":      ["TE", "APTV", "FLEX", "JBL", "AVX"],
    "Information Technology Services": ["INFY", "WIT", "ACN", "IBM", "CTSH", "TCS"],
    "Computer Hardware":          ["AAPL", "HPQ", "DELL", "HPE", "NTAP", "PSTG"],
    # Healthcare
    "Drug Manufacturers—General": ["JNJ", "PFE", "MRK", "ABBV", "BMY", "LLY", "NVO", "ROG.SW"],
    "Drug Manufacturers—Specialty & Generic": ["AMGN", "GILD", "BIIB", "REGN", "VRTX"],
    "Medical Devices":            ["MDT", "ABT", "BSX", "SYK", "ZBH", "BDX", "EW", "ISRG"],
    "Healthcare Plans":           ["UNH", "CVS", "CI", "HUM", "CNC", "MOH", "ELV"],
    "Biotechnology":              ["AMGN", "GILD", "BIIB", "REGN", "VRTX", "MRNA", "BNTX"],
    "Medical Care Facilities":    ["HCA", "THC", "UHS", "CYH"],
    "Diagnostics & Research":     ["TMO", "DHR", "IQV", "LH", "DGX", "BIO"],
    # Financials
    "Banks—Diversified":          ["JPM", "BAC", "WFC", "C", "USB", "PNC", "TFC"],
    "Banks—Regional":             ["KEY", "RF", "FITB", "HBAN", "MTB", "CFG", "ZION"],
    "Asset Management":           ["BLK", "MS", "GS", "SCHW", "TROW", "BEN", "IVZ"],
    "Insurance—Life":             ["MET", "PRU", "AFL", "LNC", "AIG"],
    "Insurance—Diversified":      ["BRK-B", "TRV", "CB", "AIG", "ALL", "HIG"],
    "Capital Markets":            ["GS", "MS", "JPM", "C", "BX", "KKR", "APO"],
    "Credit Services":            ["V", "MA", "AXP", "DFS", "COF", "SYF"],
    # Energy
    "Oil & Gas—Integrated":       ["XOM", "CVX", "SHEL", "BP", "TTE"],
    "Oil & Gas—E&P":              ["COP", "EOG", "PXD", "DVN", "MRO", "HES", "APA"],
    "Oil & Gas—Refining":         ["PSX", "MPC", "VLO", "PBF", "HFC"],
    "Oil & Gas—Midstream":        ["ET", "EPD", "WMB", "KMI", "OKE", "MPLX"],
    "Solar":                      ["ENPH", "SEDG", "FSLR", "SPWR", "RUN"],
    # Consumer Staples
    "Beverages—Non-Alcoholic":    ["KO", "PEP", "MNST", "KDRNY", "CELH"],
    "Beverages—Alcoholic":        ["BUD", "TAP", "SAM", "STZ", "MGPI"],
    "Food—Packaged":              ["PG", "KHC", "GIS", "CPB", "SJM", "CAG", "MKC", "MDLZ"],
    "Tobacco":                    ["PM", "MO", "BTI", "IMBBY"],
    "Household & Personal Products": ["PG", "CL", "CHD", "ENR", "SPB", "COTY"],
    "Grocery Stores":             ["KR", "ACI", "SFM", "WMT", "COST"],
    "Discount Stores":            ["WMT", "COST", "DG", "DLTR", "BJ"],
    # Industrials
    "Aerospace & Defense":        ["BA", "LMT", "NOC", "GD", "RTX", "HII", "L3H"],
    "Airlines":                   ["DAL", "UAL", "AAL", "LUV", "JBLU", "ALK"],
    "Industrial Conglomerates":   ["GE", "HON", "MMM", "EMR", "ITW", "ROK", "PH"],
    "Specialty Industrial Machinery": ["ITW", "PH", "ROK", "DOV", "IDEX", "XYL"],
    "Waste Management":           ["WM", "RSG", "CWST", "GFL", "ADSW"],
    "Engineering & Construction": ["EME", "MTZ", "PWR", "ACM", "STLD"],
    # Communication Services
    "Telecom Services":           ["T", "VZ", "TMUS", "DISH", "ATUS"],
    "Entertainment":              ["DIS", "NFLX", "PARA", "WBD", "FOXA", "LGF-A"],
    "Advertising Agencies":       ["IPG", "OMC", "WPP", "PUB", "DEN"],
    # Real Estate
    "REIT—Retail":                ["SPG", "O", "NNN", "MAC", "SKT", "BRX"],
    "REIT—Residential":           ["EQR", "AVB", "ESS", "MAA", "UDR", "CPT"],
    "REIT—Industrial":            ["PLD", "DRE", "EXR", "REXR", "STAG"],
    "REIT—Diversified":           ["DLR", "EQIX", "AMT", "CCI", "SBAC", "UNIT"],
    # Materials
    "Chemicals":                  ["LIN", "APD", "DD", "DOW", "EMN", "CE", "ASH"],
    "Steel":                      ["NUE", "STLD", "X", "CLF", "RS", "CMC"],
    "Mining":                     ["FCX", "NEM", "GOLD", "AEM", "WPM", "AUY", "TECK"],
    "Lumber & Wood Production":   ["WY", "PCH", "RYN", "PVH"],
    # Utilities
    "Utilities—Regulated Electric": ["NEE", "DUK", "SO", "D", "AEP", "XEL", "SRE"],
    "Utilities—Regulated Gas":    ["EQT", "SWX", "NJR", "ONE", "NW"],
    "Utilities—Renewable":        ["NEE", "ENPH", "SEDG", "FSLR", "CWEN", "BE"],
}

# Broad sector fallback
SECTOR_PEER_MAP: dict[str, list[str]] = {
    "Technology":            ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "ORCL", "CRM", "ADBE", "INTC"],
    "Consumer Cyclical":     ["AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "LOW", "CMG", "BKNG"],
    "Consumer Defensive":    ["WMT", "PG", "KO", "PEP", "COST", "PM", "MO", "CL", "MDLZ"],
    "Healthcare":            ["UNH", "JNJ", "LLY", "ABBV", "MRK", "TMO", "ABT", "DHR", "PFE"],
    "Financial Services":    ["BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "BLK"],
    "Communication Services":["GOOGL", "META", "DIS", "NFLX", "CMCSA", "T", "VZ", "SNAP"],
    "Industrials":           ["RTX", "HON", "UNP", "CAT", "GE", "BA", "LMT", "DE", "MMM"],
    "Energy":                ["XOM", "CVX", "COP", "SLB", "EOG", "PSX", "MPC", "VLO"],
    "Real Estate":           ["PLD", "AMT", "EQIX", "SPG", "CCI", "O", "DLR"],
    "Basic Materials":       ["LIN", "SHW", "APD", "ECL", "DD", "DOW", "NEM", "FCX"],
    "Utilities":             ["NEE", "DUK", "SO", "D", "AEP", "XEL", "SRE", "EXC"],
}

INDUSTRY_ALIAS_MAP: dict[str, str] = {
    "footwear accessories": "Footwear & Accessories",
    "footwear and accessories": "Footwear & Accessories",
    "apparel": "Apparel Retail",
    "apparel manufacturing": "Apparel Retail",
    "textile manufacturing": "Apparel Retail",
    "internet retailing": "Internet Retail",
    "software application": "Software—Application",
    "software infrastructure": "Software—Infrastructure",
    "drug manufacturers general": "Drug Manufacturers—General",
    "drug manufacturers specialty generic": "Drug Manufacturers—Specialty & Generic",
    # Electrical Equipment old key (legacy) → canonical
    "electrical equipment": "Electrical Equipment & Parts",
    "electrical components": "Electrical Equipment & Parts",
    "electrical equipment parts": "Electrical Equipment & Parts",
    "electronic equipment": "Electrical Equipment & Parts",
    "lighting": "Electrical Equipment & Parts",
    "lighting equipment": "Electrical Equipment & Parts",
    # Staffing variants
    "staffing": "Staffing & Employment Services",
    "employment services": "Staffing & Employment Services",
    "staffing employment": "Staffing & Employment Services",
}

_INDUSTRY_STOPWORDS = {
    "and",
    "general",
    "goods",
    "group",
    "holdings",
    "manufacturing",
    "products",
    "retail",
    "services",
    "specialty",
}

_EXCHANGE_SUFFIX_MAP = {
    "AMEX": "",
    "ARCA": "",
    "AS": ".AS",
    "AU": ".AX",
    "BATS": "",
    "DE": ".DE",
    "HK": ".HK",
    "KO": ".KS",
    "LSE": ".L",
    "MI": ".MI",
    "NASDAQ": "",
    "NYSE": "",
    "NYSEARCA": "",
    "NYSEAMERICAN": "",
    "PA": ".PA",
    "SW": ".SW",
    "TSE": ".T",
    "US": "",
    "XETRA": ".DE",
}

_INDUSTRY_KEY_LOOKUP = {
    re.sub(r"[^a-z0-9]+", " ", key.lower()).strip(): key for key in INDUSTRY_PEER_MAP
}


def _normalize_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _industry_tokens(industry: str) -> set[str]:
    return {
        token
        for token in _normalize_label(industry).split()
        if token and token not in _INDUSTRY_STOPWORDS
    }


def _match_industry_key(industry: str) -> str | None:
    taxonomy = resolve_industry_taxonomy(industry)
    canonical = str(taxonomy.get("canonical_industry") or "").strip()
    if canonical in INDUSTRY_PEER_MAP:
        return canonical

    normalized = _normalize_label(industry)
    if not normalized:
        return None
    direct = _INDUSTRY_KEY_LOOKUP.get(normalized)
    if direct:
        return direct
    alias = INDUSTRY_ALIAS_MAP.get(normalized)
    if alias:
        return alias

    subject_tokens = _industry_tokens(industry)
    if not subject_tokens:
        return None

    best_key: str | None = None
    best_score = 0.0
    for key in INDUSTRY_PEER_MAP:
        key_tokens = _industry_tokens(key)
        if not key_tokens:
            continue
        overlap = len(subject_tokens & key_tokens)
        if not overlap:
            continue
        score = overlap / len(subject_tokens | key_tokens)
        if score > best_score:
            best_key = key
            best_score = score
    return best_key if best_score >= 0.5 else None


def _dedupe_preserve(items: list[str], *, exclude: set[str] | None = None, limit: int = 12) -> list[str]:
    blocked = {item.upper() for item in (exclude or set())}
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in items:
        symbol = (item or "").upper()
        if not symbol or symbol in blocked or symbol in seen:
            continue
        seen.add(symbol)
        cleaned.append(symbol)
        if len(cleaned) >= limit:
            break
    return cleaned


def _ticker_variants(ticker: str) -> set[str]:
    symbol = (ticker or "").upper()
    if not symbol:
        return set()
    base = symbol.split(".", 1)[0]
    return {symbol, base}


def _normalize_exchange_ticker(code: str, exchange: str) -> str | None:
    symbol = (code or "").upper().strip()
    venue = (exchange or "").upper().strip()
    if not symbol:
        return None
    if "." in symbol:
        base, dotted_exchange = symbol.split(".", 1)
        suffix = _EXCHANGE_SUFFIX_MAP.get(dotted_exchange)
        if suffix is None:
            return symbol
        return f"{base}{suffix}"
    suffix = _EXCHANGE_SUFFIX_MAP.get(venue)
    if suffix is None:
        return symbol if venue in {"", "OTC", "PINK"} else None
    return f"{symbol}{suffix}"


@lru_cache(maxsize=1)
def _load_cached_peer_profiles() -> tuple[dict[str, Any], ...]:
    cache_dir = Path(__file__).with_name("cache")
    snapshot_path = cache_dir / "_peer_profiles.pkl"
    snapshot_ttl_sec = 6 * 3600.0  # 6 hours — same TTL as _build_eodhd_multiples_index

    # Fast path: load from pickle snapshot to avoid scanning 12k+ JSON files.
    try:
        import pickle as _pickle
        import time as _t
        if snapshot_path.exists():
            age = _t.time() - snapshot_path.stat().st_mtime
            if age < snapshot_ttl_sec:
                with snapshot_path.open("rb") as _f:
                    snap = _pickle.load(_f)
                if isinstance(snap, tuple) and snap:
                    return snap
    except Exception:
        pass

    profiles: list[dict[str, Any]] = []
    for path in cache_dir.glob("eodhd_fund_*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        payload = raw.get("data") if isinstance(raw, dict) and "data" in raw else raw
        if not isinstance(payload, dict):
            continue

        general = payload.get("General") or {}
        highlights = payload.get("Highlights") or {}
        code = general.get("Code") or path.stem.replace("eodhd_fund_", "").replace("_", ".")
        exchange = general.get("Exchange") or ""
        ticker = _normalize_exchange_ticker(str(code), str(exchange))
        if not ticker:
            continue

        profiles.append(
            {
                "ticker": ticker,
                "variants": _ticker_variants(ticker) | _ticker_variants(str(code)),
                "sector": str(general.get("Sector") or ""),
                "industry": str(general.get("Industry") or ""),
                "exchange": str(exchange),
                "market_cap_mln": float(highlights.get("MarketCapitalizationMln") or 0.0),
            }
        )

    result = tuple(profiles)
    # Persist snapshot so subsequent cold starts skip the JSON scan.
    try:
        import pickle as _pickle
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = snapshot_path.with_suffix(".pkl.tmp")
        with tmp.open("wb") as _f:
            _pickle.dump(result, _f, protocol=_pickle.HIGHEST_PROTOCOL)
        tmp.replace(snapshot_path)
    except Exception:
        pass
    return result


def _discover_cached_peers(
    ticker: str,
    sector: str,
    industry: str,
    *,
    max_peers: int = 12,
    include_related: bool = False,
) -> list[str]:
    profiles = _load_cached_peer_profiles()
    if not profiles:
        return []

    subject_variants = _ticker_variants(ticker)
    subject_profile = next(
        (profile for profile in profiles if subject_variants & set(profile.get("variants") or set())),
        None,
    )
    subject_market_cap = float((subject_profile or {}).get("market_cap_mln") or 0.0)
    subject_exchange = str((subject_profile or {}).get("exchange") or "")
    subject_sector = _normalize_label(sector)
    subject_taxonomy = resolve_industry_taxonomy(industry, sector)
    subject_related_industries = set(subject_taxonomy.get("related_industries") or [])

    scored: list[tuple[float, str]] = []
    for profile in profiles:
        candidate_ticker = str(profile.get("ticker") or "")
        if not candidate_ticker:
            continue
        if subject_variants & set(profile.get("variants") or set()):
            continue
        if subject_sector and _normalize_label(str(profile.get("sector") or "")) != subject_sector:
            continue

        candidate_industry = str(profile.get("industry") or "")
        candidate_sector = str(profile.get("sector") or "")
        candidate_taxonomy = resolve_industry_taxonomy(candidate_industry, candidate_sector)
        similarity = industry_similarity(
            industry,
            candidate_industry,
            subject_sector=sector,
            candidate_sector=candidate_sector,
        )
        same_canonical = bool(
            subject_taxonomy.get("canonical_industry")
            and subject_taxonomy.get("canonical_industry") == candidate_taxonomy.get("canonical_industry")
        )
        related_match = bool(
            candidate_taxonomy.get("canonical_industry") in subject_related_industries
            or subject_taxonomy.get("canonical_industry") in set(candidate_taxonomy.get("related_industries") or [])
        )
        same_family = bool(
            subject_taxonomy.get("family")
            and subject_taxonomy.get("family") == candidate_taxonomy.get("family")
        )
        if not same_canonical:
            if not include_related:
                continue
            if related_match:
                if similarity < 0.45:
                    continue
            elif not same_family or similarity < 0.70:
                continue

        score = 100.0 if same_canonical else similarity * 60.0
        if subject_exchange and str(profile.get("exchange") or "") == subject_exchange:
            score += 5.0

        candidate_market_cap = float(profile.get("market_cap_mln") or 0.0)
        if subject_market_cap > 0 and candidate_market_cap > 0:
            market_cap_gap = abs(math.log10(max(candidate_market_cap, 1.0) / max(subject_market_cap, 1.0)))
            score -= min(15.0, market_cap_gap * 10.0)

        scored.append((score, candidate_ticker))

    scored.sort(key=lambda item: item[0], reverse=True)
    return _dedupe_preserve([ticker for _, ticker in scored], exclude=subject_variants, limit=max_peers)


def _industry_peers(industry: str, *, include_related: bool = False) -> list[str]:
    canonical_industry = str(resolve_industry_taxonomy(industry).get("canonical_industry") or "").strip()
    if canonical_industry and canonical_industry in INDUSTRY_PEER_MAP:
        canonical_peers = list(INDUSTRY_PEER_MAP.get(canonical_industry) or [])
        if not include_related:
            return canonical_peers

        peer_lists: list[str] = list(canonical_peers)
        for key in related_industries(industry):
            if key == canonical_industry:
                continue
            peer_lists.extend(list(INDUSTRY_PEER_MAP.get(key) or []))
        return _dedupe_preserve(peer_lists)

    key = _match_industry_key(industry)
    if key:
        return list(INDUSTRY_PEER_MAP.get(key) or [])
    return []


@lru_cache(maxsize=256)
def _curated_industry_for_ticker(ticker: str) -> str:
    ticker_text = str(ticker or "").strip().upper()
    if not ticker_text:
        return ""
    for industry_name, peers in INDUSTRY_PEER_MAP.items():
        if ticker_text in {str(peer or "").strip().upper() for peer in peers}:
            return str(industry_name)
    return ""


def _resolve_ticker_industry_metadata(
    ticker: str,
    profiles: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return best-effort {sector, industry, canonical_industry} for *ticker*.

    Resolution order:
    1. Local cached fund profile (most reliable, has real EODHD data)
    2. Curated INDUSTRY_PEER_MAP reverse-lookup (covers international names not in cache)
    3. Taxonomy inference from canonical_industry
    """
    if profiles is None:
        profiles = {str(p.get("ticker") or ""): p for p in _load_cached_peer_profiles()}

    candidate = str(ticker or "").upper()
    profile = profiles.get(candidate) or {}
    sector = str(profile.get("sector") or "").strip()
    industry = str(profile.get("industry") or "").strip()

    if not industry:
        industry = _curated_industry_for_ticker(candidate)

    if industry and not sector:
        taxonomy = resolve_industry_taxonomy(industry)
        sector = str(taxonomy.get("canonical_sector") or "").strip()

    canonical_industry = ""
    if industry:
        taxonomy = resolve_industry_taxonomy(industry, sector)
        canonical_industry = str(taxonomy.get("canonical_industry") or industry).strip()

    return {"sector": sector, "industry": industry, "canonical_industry": canonical_industry}


def _safe_universe_store() -> Any | None:
    try:
        from auto_valuation.learning.universe import SymbolUniverseStore

        return SymbolUniverseStore()
    except Exception:
        return None


def _safe_discovery_store() -> Any | None:
    try:
        from auto_valuation.learning.discovery import DiscoveryStore

        return DiscoveryStore()
    except Exception:
        return None


def _peer_learning_bonus(symbol: dict[str, Any] | None) -> float:
    if not symbol:
        return 0.0
    metadata = dict(symbol.get("metadata") or {})
    bonus = 0.0
    bonus += min(float(metadata.get("compare_hits") or 0.0) * 0.9, 4.0)
    bonus += min(float(metadata.get("watchlist_hits") or 0.0) * 0.6, 2.5)
    bonus += min(float(metadata.get("selection_hits") or 0.0) * 0.25, 1.0)
    bonus += min(float(metadata.get("peer_candidate_hits") or 0.0) * 0.12, 1.0)
    bonus += min(float(symbol.get("valuation_hits") or 0.0) * 0.15, 1.0)
    if symbol.get("fundamentals_cached"):
        bonus += 0.3
    return bonus


def _pair_relationship_context(subject_ticker: str, peer_ticker: str, discovery_store: Any | None) -> dict[str, Any]:
    default = {
        "pair_strength_score": 0.0,
        "pair_hits": 0,
        "pair_auto_peer_hits": 0,
        "pair_manual_compare_hits": 0,
        "pair_last_seen_at": "",
    }
    if discovery_store is None:
        return default
    try:
        relationship = discovery_store.get_peer_relationship(subject_ticker, peer_ticker)
    except Exception:
        relationship = None
    if not relationship:
        return default
    return {
        "pair_strength_score": round(float(relationship.get("pair_strength_score") or 0.0), 4),
        "pair_hits": int(relationship.get("pair_hits") or 0),
        "pair_auto_peer_hits": int(relationship.get("auto_peer_hits") or 0),
        "pair_manual_compare_hits": int(relationship.get("manual_compare_hits") or 0),
        "pair_last_seen_at": str(relationship.get("last_seen_at") or ""),
    }


def _rank_peer_tickers(
    peer_tickers: list[str],
    *,
    subject_ticker: str,
    sector: str,
    industry: str,
) -> list[str]:
    if not peer_tickers:
        return []

    subject_variants = _ticker_variants(subject_ticker)
    profiles = {str(profile.get("ticker") or ""): profile for profile in _load_cached_peer_profiles()}
    subject_profile = next(
        (profile for profile in profiles.values() if subject_variants & set(profile.get("variants") or set())),
        None,
    )
    subject_market_cap = float((subject_profile or {}).get("market_cap_mln") or 0.0)
    subject_exchange = str((subject_profile or {}).get("exchange") or "")
    universe_store = _safe_universe_store()
    discovery_store = _safe_discovery_store()

    scored: list[tuple[float, int, str]] = []
    for index, ticker in enumerate(peer_tickers):
        candidate = str(ticker or "").upper()
        # Resolve best-effort metadata: cached profile first, then curated map fallback.
        meta = _resolve_ticker_industry_metadata(candidate, profiles)
        candidate_sector = meta["sector"]
        candidate_industry = meta["industry"]

        similarity = industry_similarity(
            industry,
            candidate_industry,
            subject_sector=sector,
            candidate_sector=candidate_sector,
        )

        # ── Hard taxonomy gate ──────────────────────────────────────────────
        # Learning bonuses may ONLY reorder peers that already pass the gate.
        # Peers with no industry metadata or similarity below the minimum are
        # excluded from the displayed basket entirely.
        if similarity < _PEER_MIN_INDUSTRY_FIT:
            logger.debug(
                "Peer %s excluded (industry_similarity=%.3f < %.2f) for subject %s / %s",
                candidate,
                similarity,
                _PEER_MIN_INDUSTRY_FIT,
                ticker,
                industry,
            )
            continue

        score = similarity * 60.0

        candidate_exchange = str((profiles.get(candidate) or {}).get("exchange") or "")
        if subject_exchange and candidate_exchange and candidate_exchange == subject_exchange:
            score += 4.0

        candidate_market_cap = float((profiles.get(candidate) or {}).get("market_cap_mln") or 0.0)
        if subject_market_cap > 0 and candidate_market_cap > 0:
            market_cap_gap = abs(math.log10(max(candidate_market_cap, 1.0) / max(subject_market_cap, 1.0)))
            score += max(0.0, 8.0 - market_cap_gap * 8.0)

        # Learning bonuses applied AFTER the taxonomy gate — they reorder
        # valid peers, never rescue invalid ones.
        if universe_store is not None:
            score += _peer_learning_bonus(universe_store.get_symbol(candidate))

        pair_context = _pair_relationship_context(subject_ticker, candidate, discovery_store)
        score += min(float(pair_context["pair_strength_score"]) * 4.0, 18.0)

        scored.append((round(score, 4), index, candidate))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [ticker for _, _, ticker in scored]


def _enrich_peer_rows(
    peers: list[dict],
    *,
    target_ticker: str,
    peer_tickers: list[str],
    target_sector: str = "",
    target_industry: str = "",
) -> list[dict]:
    if not peers:
        return []

    profiles = {str(profile.get("ticker") or ""): profile for profile in _load_cached_peer_profiles()}
    target_variants = _ticker_variants(target_ticker)
    target_profile = next(
        (profile for profile in profiles.values() if target_variants & set(profile.get("variants") or set())),
        None,
    )
    resolved_target_sector = str(target_sector or (target_profile or {}).get("sector") or "")
    resolved_target_industry = str(target_industry or (target_profile or {}).get("industry") or "")
    target_taxonomy = resolve_industry_taxonomy(resolved_target_industry, resolved_target_sector)
    order_map = {str(ticker or "").upper(): index for index, ticker in enumerate(peer_tickers)}
    universe_store = _safe_universe_store()
    discovery_store = _safe_discovery_store()

    enriched: list[dict] = []
    for peer in peers:
        row = dict(peer)
        ticker = str(row.get("ticker") or row.get("symbol") or "").upper()
        profile = profiles.get(ticker) or {}
        curated_industry = _curated_industry_for_ticker(ticker)
        row["ticker"] = ticker
        row["exchange"] = str(row.get("exchange") or profile.get("exchange") or "")
        row["sector"] = str(row.get("sector") or profile.get("sector") or "")
        row["industry"] = str(row.get("industry") or profile.get("industry") or curated_industry or "")
        if not row["sector"] and row["industry"]:
            row["sector"] = str(resolve_industry_taxonomy(row["industry"], resolved_target_sector).get("canonical_sector") or "")
        taxonomy = resolve_industry_taxonomy(row["industry"], row["sector"])
        row["industry_similarity"] = round(
            industry_similarity(
                resolved_target_industry,
                row["industry"],
                subject_sector=resolved_target_sector,
                candidate_sector=row["sector"],
            )
            if resolved_target_industry and row["industry"]
            else 0.0,
            4,
        )
        global_peer_score = _peer_learning_bonus(universe_store.get_symbol(ticker)) if universe_store is not None else 0.0
        pair_context = _pair_relationship_context(target_ticker, ticker, discovery_store)
        row["canonical_industry"] = str(taxonomy.get("canonical_industry") or row["industry"] or "")
        row["industry_family"] = str(taxonomy.get("family") or "")
        same_canonical = bool(
            target_taxonomy.get("canonical_industry")
            and target_taxonomy.get("canonical_industry") == taxonomy.get("canonical_industry")
            and str(taxonomy.get("canonical_industry") or "").strip()
        )
        row["same_industry_cluster"] = bool(
            target_taxonomy.get("cluster_id")
            and target_taxonomy.get("cluster_id") == taxonomy.get("cluster_id")
            and str(taxonomy.get("cluster_id") or "").strip()
        )
        row["same_industry_family"] = bool(
            target_taxonomy.get("family")
            and target_taxonomy.get("family") == taxonomy.get("family")
            and str(taxonomy.get("family") or "").strip()
        )
        row["industry_fit_score"] = round(row["industry_similarity"] * 5.0, 4)
        row["global_peer_score"] = round(global_peer_score, 4)
        row["pair_strength_score"] = round(float(pair_context["pair_strength_score"]), 4)
        row["pair_hits"] = int(pair_context["pair_hits"])
        row["pair_auto_peer_hits"] = int(pair_context["pair_auto_peer_hits"])
        row["pair_manual_compare_hits"] = int(pair_context["pair_manual_compare_hits"])
        row["pair_last_seen_at"] = str(pair_context["pair_last_seen_at"])
        row["base_peer_learning_score"] = round(row["industry_fit_score"] + row["global_peer_score"], 4)
        row["peer_learning_score"] = round(row["base_peer_learning_score"] + row["pair_strength_score"], 4)
        row["peer_rank"] = int(order_map.get(ticker, len(order_map)))

        # ── Audit fields ─────────────────────────────────────────────────────
        industry_sim = row["industry_similarity"]
        has_industry_meta = bool(row["industry"]) and bool(row["sector"])
        if not has_industry_meta:
            row["peer_valid"] = False
            row["peer_classification"] = "cross-sector-analog"
            row["pass_reason"] = "missing-metadata"
            row["fallback_reason"] = "sector or industry metadata could not be resolved"
        elif industry_sim >= 0.85 and same_canonical:
            row["peer_valid"] = True
            row["peer_classification"] = "competitor"
            row["pass_reason"] = "same-canonical-industry"
            row["fallback_reason"] = ""
        elif industry_sim >= _PEER_MIN_INDUSTRY_FIT:
            row["peer_valid"] = True
            row["peer_classification"] = "related-reaction"
            row["pass_reason"] = "related-industry" if not same_canonical else "same-canonical-industry"
            row["fallback_reason"] = "" if same_canonical else "related-industry fallback"
        else:
            row["peer_valid"] = False
            row["peer_classification"] = "cross-sector-analog"
            row["pass_reason"] = "failed-taxonomy-gate"
            row["fallback_reason"] = f"industry_similarity={industry_sim:.3f} below minimum {_PEER_MIN_INDUSTRY_FIT}"

        enriched.append(row)

    enriched.sort(
        key=lambda row: (
            int(row["peer_rank"]) if row.get("peer_rank") is not None else len(order_map),
            -int(row.get("market_cap") or 0),
            str(row.get("ticker") or ""),
        )
    )
    return enriched


def _sector_peers(sector: str) -> list[str]:
    for key, peers in SECTOR_PEER_MAP.items():
        if sector and key.lower() == sector.lower():
            return list(peers)
    return []


# ─── Public API ───────────────────────────────────────────────────────────────

def get_peers_for_ticker(
    ticker: str,
    sector: str = "",
    industry: str = "",
) -> list[str]:
    """Return peer tickers for *ticker*.

    Priority: multi-segment override → same-canonical-industry cached peers +
    curated industry map → related-industry fallback (capped at
    _PEER_MAX_RELATED_FALLBACK) → sector fallback.

    The taxonomy gate in _rank_peer_tickers ensures every returned ticker has
    an industry similarity score ≥ _PEER_MIN_INDUSTRY_FIT.  Tickers that fail
    the gate are silently excluded — they never reach the displayed basket.

    Self-ticker is always removed.  Max 12 unique peers returned.
    """
    ticker = ticker.upper()
    # Build the full exclusion set: self-ticker variants + known cross-listed same-company tickers
    _self_exclude = _ticker_variants(ticker) | _CROSS_LISTED_EXCLUSIONS.get(ticker, set())

    # 1. Multi-segment override (no taxonomy gate applied — basket is curated).
    if ticker in MULTI_SEGMENT_PEERS:
        seen: set[str] = set()
        peers: list[str] = []
        for seg_peers in MULTI_SEGMENT_PEERS[ticker].values():
            for p in seg_peers:
                if p.upper() not in _self_exclude and p not in seen:
                    seen.add(p)
                    peers.append(p)
        return peers[:12]

    # 2. Same-canonical-industry: cached discovery + curated map (primary basket).
    cached_industry_peers = _discover_cached_peers(ticker, sector, industry, max_peers=12, include_related=False)
    curated_industry_peers = _industry_peers(industry, include_related=False)
    industry_peers = _dedupe_preserve(
        cached_industry_peers + curated_industry_peers,
        exclude=_self_exclude,
        limit=12,
    )
    ranked = _rank_peer_tickers(industry_peers, subject_ticker=ticker, sector=sector, industry=industry)
    if len(ranked) >= 4:
        return ranked

    # 3. Related-industry fallback — capped so unrelated names cannot flood the basket.
    related_cached_peers = _discover_cached_peers(ticker, sector, industry, max_peers=8, include_related=True)
    # Separate same-canonical from related-only curated peers so we can cap the
    # related portion independently.
    curated_same = set(curated_industry_peers)
    all_related_curated = _industry_peers(industry, include_related=True)
    related_only_curated = [p for p in all_related_curated if p not in curated_same][:_PEER_MAX_RELATED_FALLBACK]
    combined = _dedupe_preserve(
        industry_peers + related_cached_peers + related_only_curated,
        exclude=_self_exclude,
        limit=12,
    )
    ranked_combined = _rank_peer_tickers(combined, subject_ticker=ticker, sector=sector, industry=industry)
    if ranked_combined:
        return ranked_combined

    if str(industry or "").strip():
        return []

    # 4. Sector fallback — only when no industry was supplied at all.
    sector_peers = _dedupe_preserve(_sector_peers(sector), exclude=_self_exclude, limit=8)
    if sector_peers:
        return _rank_peer_tickers(sector_peers, subject_ticker=ticker, sector=sector, industry=industry)

    return []


def get_segment_peers(ticker: str) -> dict[str, list[str]]:
    """Return segment-level peer breakdown if available, else {}."""
    return MULTI_SEGMENT_PEERS.get(ticker.upper(), {})


# ─── Live metric fetching ─────────────────────────────────────────────────────

def _resolve_cache_dir() -> str | None:
    candidates = [
        Path(__file__).with_name("cache"),
        Path(tempfile.gettempdir()) / "nelix-capital-cache",
    ]
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return str(candidate)
        except OSError:
            continue
    logger.warning("Peer disk cache disabled: no writable cache directory available.")
    return None


_CACHE_DIR = _resolve_cache_dir()
_CACHE_TTL  = 86_400  # 24 hours


def _cache_path(ticker: str) -> str | None:
    if not _CACHE_DIR:
        return None
    return os.path.join(_CACHE_DIR, f"{ticker.upper()}_peers.json")


def _load_cache(ticker: str) -> list | None:
    p = _cache_path(ticker)
    if not p or not os.path.exists(p):
        return None
    age = time.time() - os.path.getmtime(p)
    if age > _CACHE_TTL:
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(ticker: str, data: list) -> None:
    p = _cache_path(ticker)
    if not p:
        return
    try:
        with open(p, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _peer_cache_key(target_ticker: str, peer_tickers: list[str]) -> str:
    signature = ",".join(sorted({ticker.upper() for ticker in peer_tickers if ticker}))
    digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:12]
    return f"{target_ticker.upper()}_{digest}"


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        import math
        f = float(val)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        return default


def _safe_multiple(val: Any, max_val: float = 500.0) -> float | None:
    """Return val as a positive multiple or None if unreasonable."""
    try:
        v = float(val or 0)
    except (TypeError, ValueError):
        return None
    if v <= 0 or v > max_val:
        return None
    return round(v, 2)


_PEER_MULTIPLE_KEYS = ("ev_rev", "ev_ebitda", "ev_ebit", "pe", "p_fcf")


def _row_has_peer_multiple(row: dict[str, Any]) -> bool:
    return any(row.get(key) and row.get(key) > 0 for key in _PEER_MULTIPLE_KEYS)


def _peer_cache_needs_refresh(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return True
    return any(str(row.get("source") or "").lower() == "not_available" for row in rows)


def _first_number(*values: Any, default: float = 0.0) -> float:
    for value in values:
        try:
            if value is None or value == "" or value == "None":
                continue
            numeric = float(value)
            if not math.isnan(numeric) and not math.isinf(numeric):
                return numeric
        except (TypeError, ValueError):
            continue
    return default


def _latest_yearly(financials: dict[str, Any], section_name: str) -> dict[str, Any]:
    section = financials.get(section_name) or {}
    yearly = section.get("yearly") or {}
    if not isinstance(yearly, dict) or not yearly:
        return {}
    for _key, row in sorted(yearly.items(), key=lambda item: str(item[0]), reverse=True):
        if isinstance(row, dict) and row:
            return row
    return {}


def _metrics_from_eodhd_fundamentals(
    payload: dict[str, Any] | None,
    *,
    fallback_ticker: str = "",
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if "data" in payload else payload
    if not isinstance(data, dict):
        return None

    general = data.get("General") or {}
    highlights = data.get("Highlights") or {}
    valuation = data.get("Valuation") or {}
    financials = data.get("Financials") or {}

    code = str(general.get("Code") or "").strip()
    exchange = str(general.get("Exchange") or "").strip()
    if not code and fallback_ticker:
        code = fallback_ticker.split(".", 1)[0]
        if "." in fallback_ticker and not exchange:
            exchange = fallback_ticker.split(".", 1)[1]
    if not code:
        return None

    income = _latest_yearly(financials, "Income_Statement")
    cash_flow = _latest_yearly(financials, "Cash_Flow")
    balance = _latest_yearly(financials, "Balance_Sheet")

    market_cap_mln = _first_number(highlights.get("MarketCapitalizationMln"))
    market_cap_raw = _first_number(highlights.get("MarketCapitalization"), general.get("MarketCapitalization"))
    if market_cap_mln <= 0 and market_cap_raw > 0:
        market_cap_mln = market_cap_raw / 1e6 if market_cap_raw > 1e6 else market_cap_raw
    if market_cap_raw <= 0 and market_cap_mln > 0:
        market_cap_raw = market_cap_mln * 1e6

    ev_raw = _first_number(valuation.get("EnterpriseValue"))
    ev_mln = _first_number(valuation.get("EnterpriseValueMln"), valuation.get("EnterpriseValueMRQ"))
    if ev_raw <= 0 and ev_mln > 0:
        ev_raw = ev_mln * 1e6

    cash = _first_number(
        balance.get("cashAndShortTermInvestments"),
        balance.get("cashAndCashEquivalents"),
        balance.get("cash"),
    )
    debt = _first_number(balance.get("totalDebt"))
    if debt <= 0:
        debt = _first_number(balance.get("shortTermDebt")) + _first_number(balance.get("longTermDebt"))
    if ev_raw <= 0 and market_cap_raw > 0:
        ev_raw = max(0.0, market_cap_raw + debt - cash)

    revenue = _first_number(income.get("totalRevenue"), income.get("revenue"))
    ebit = _first_number(income.get("operatingIncome"), income.get("ebit"))
    ebitda = _first_number(income.get("ebitda"), income.get("EBITDA"))
    depreciation = abs(_first_number(income.get("depreciationAndAmortization"), cash_flow.get("depreciationAndAmortization")))
    if ebitda <= 0 and ebit > 0 and depreciation > 0:
        ebitda = ebit + depreciation
    net_income = _first_number(income.get("netIncome"), income.get("net_income"))

    free_cash_flow = _first_number(cash_flow.get("freeCashFlow"), cash_flow.get("free_cash_flow"))
    if free_cash_flow <= 0:
        operating_cash_flow = _first_number(cash_flow.get("totalCashFromOperatingActivities"), cash_flow.get("operatingCashFlow"))
        capex = _first_number(cash_flow.get("capitalExpenditures"), cash_flow.get("capitalExpenditure"))
        if operating_cash_flow > 0:
            free_cash_flow = operating_cash_flow + capex if capex < 0 else operating_cash_flow - abs(capex)

    ev_rev = _safe_multiple(_first_number(valuation.get("EnterpriseValueRevenue"), valuation.get("EVToRevenue")))
    ev_ebitda = _safe_multiple(_first_number(valuation.get("EnterpriseValueEbitda"), valuation.get("EnterpriseValueEBITDA"), valuation.get("EVToEBITDA")))
    ev_ebit = _safe_multiple(_first_number(valuation.get("EnterpriseValueEbit"), valuation.get("EnterpriseValueEBIT"), valuation.get("EVToEBIT")))
    pe = _safe_multiple(_first_number(highlights.get("PERatio"), valuation.get("TrailingPE"), valuation.get("PE")))
    p_fcf = _safe_multiple(_first_number(valuation.get("PriceToFreeCashFlow"), valuation.get("PriceFreeCashFlow"), valuation.get("PFCF")))

    if ev_raw > 0:
        ev_rev = ev_rev or _safe_multiple(ev_raw / revenue if revenue > 0 else None)
        ev_ebitda = ev_ebitda or _safe_multiple(ev_raw / ebitda if ebitda > 0 else None)
        ev_ebit = ev_ebit or _safe_multiple(ev_raw / ebit if ebit > 0 else None)
    if market_cap_raw > 0:
        pe = pe or _safe_multiple(market_cap_raw / net_income if net_income > 0 else None)
        p_fcf = p_fcf or _safe_multiple(market_cap_raw / free_cash_flow if free_cash_flow > 0 else None)

    eodhd_code = f"{code}.{exchange}" if exchange else code
    variants = _ticker_variants(eodhd_code) | _ticker_variants(code) | _ticker_variants(fallback_ticker)
    return {
        "ticker": eodhd_code.upper(),
        "name": str(general.get("Name") or code),
        "market_cap": round(market_cap_mln),
        "ev": round(ev_raw / 1e6) if ev_raw else 0,
        "revenue": revenue or None,
        "ebitda": ebitda or None,
        "ebit": ebit or None,
        "net_income": net_income or None,
        "fcf": free_cash_flow or None,
        "ev_rev": ev_rev,
        "ev_ebitda": ev_ebitda,
        "ev_ebit": ev_ebit,
        "pe": pe,
        "p_fcf": p_fcf,
        "sector": str(general.get("Sector") or ""),
        "industry": str(general.get("Industry") or ""),
        "exchange": exchange,
        "source": "eodhd",
        "_variants": {variant.upper() for variant in variants if variant},
    }


def _fetch_eodhd_fundamentals_for_peer(ticker: str) -> dict[str, Any] | None:
    try:
        from webapp.data.eodhd_client import _fetch_fundamentals, normalize_requested_ticker
    except Exception:
        return None

    candidates: list[str] = []
    try:
        candidates.append(normalize_requested_ticker(ticker))
    except Exception:
        pass
    if ticker:
        candidates.append(str(ticker).upper())

    seen: set[str] = set()
    for candidate in candidates:
        code = str(candidate or "").upper().strip()
        if not code or code in seen:
            continue
        seen.add(code)
        try:
            fundamentals = _fetch_fundamentals(code)
        except Exception:
            fundamentals = None
        if isinstance(fundamentals, dict):
            return fundamentals
    return None


def _lookup_eodhd_peer_metrics(ticker: str, eodhd_index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    ticker_text = str(ticker or "").upper().strip()
    for variant in _ticker_variants(ticker_text):
        metrics = eodhd_index.get(variant)
        if metrics:
            return dict(metrics)

    fundamentals = _fetch_eodhd_fundamentals_for_peer(ticker_text)
    metrics = _metrics_from_eodhd_fundamentals(fundamentals, fallback_ticker=ticker_text)
    if not metrics:
        return None

    variants = metrics.pop("_variants", set())
    for variant in variants:
        eodhd_index.setdefault(str(variant).upper(), dict(metrics))
    return dict(metrics)


@lru_cache(maxsize=1)
def _build_eodhd_multiples_index() -> dict[str, dict[str, Any]]:
    """Scan all cached EODHD fundamentals files and build a
    {ticker_variant → multiples_dict} index.

    Used by fetch_peer_metrics so that international peers (e.g. ABBN.SW,
    LR.PA) get real multiples from EODHD instead of empty yfinance responses.

    ADAPTIVE_DCF_IMPROVEMENT_PLAN.md (M4) — pickled to disk so cold starts
    skip the multi-thousand-file JSON scan when the snapshot is fresh.
    """
    cache_dir = Path(__file__).with_name("cache")
    snapshot_path = cache_dir / "_peer_index.pkl"
    snapshot_ttl_sec = 6 * 3600.0  # 6 hours

    # Try fast-path disk snapshot first.
    try:
        import pickle, time as _t
        if snapshot_path.exists():
            age = _t.time() - snapshot_path.stat().st_mtime
            if age < snapshot_ttl_sec:
                with snapshot_path.open("rb") as f:
                    snap = pickle.load(f)
                if isinstance(snap, dict) and snap:
                    logger.debug("EODHD multiples index loaded from disk (%d variants, age=%.0fs)", len(snap), age)
                    return snap
    except Exception as exc:
        logger.warning("peer index disk load failed: %s", exc)

    index: dict[str, dict[str, Any]] = {}

    for path in cache_dir.glob("eodhd_fund_*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        payload = raw.get("data") if isinstance(raw, dict) and "data" in raw else raw
        if not isinstance(payload, dict):
            continue

        stem = path.stem.replace("eodhd_fund_", "").replace("_", ".")
        metrics = _metrics_from_eodhd_fundamentals(payload, fallback_ticker=stem)
        if not metrics:
            continue
        variants = metrics.pop("_variants", set())
        for variant in variants:
            if variant:
                index.setdefault(variant.upper(), metrics)

    logger.debug("EODHD multiples index built: %d ticker variants", len(index))
    # M4 — persist snapshot so subsequent cold starts skip the JSON scan.
    try:
        import pickle
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = snapshot_path.with_suffix(".pkl.tmp")
        with tmp.open("wb") as f:
            pickle.dump(index, f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(snapshot_path)
    except Exception as exc:
        logger.warning("peer index disk save failed: %s", exc)
    return index


def fetch_peer_metrics(
    peer_tickers: list[str],
    target_ticker: str,
    timeout_per_peer: float = 6.0,
    target_sector: str = "",
    target_industry: str = "",
) -> tuple[list[dict], dict]:
    """Fetch basic valuation metrics for *peer_tickers*.

    Data source priority:
    1. EODHD fundamentals cache/index (covers all international tickers)
    2. Live EODHD fundamentals fetch for peers missing from the local index
    3. N/A only when EODHD has no fundamentals for that peer

    Returns:
        peers       — list of peer dicts (one per ticker, sorted by mkt cap desc)
        peer_median — dict of median multiples across the peer group
    """
    target_ticker = target_ticker.upper()
    cache_key = _peer_cache_key(target_ticker, peer_tickers)

    cached = _load_cache(cache_key)
    if cached is not None and not _peer_cache_needs_refresh(list(cached)):
        logger.debug("Using cached peer data for %s", cache_key)
        peers = _enrich_peer_rows(
            list(cached),
            target_ticker=target_ticker,
            peer_tickers=peer_tickers,
            target_sector=target_sector,
            target_industry=target_industry,
        )
        # Invalidate stale invalid rows so they don't persist in the displayed basket.
        peers = [p for p in peers if p.get("subject") or p.get("peer_valid", True)]
        peer_median = _compute_median(peers, target_ticker)
        return peers, peer_median
    if cached is not None:
        logger.info("Refreshing stale peer cache with missing multiples for %s", cache_key)

    eodhd_index = _build_eodhd_multiples_index()
    profiles = {str(profile.get("ticker") or ""): profile for profile in _load_cached_peer_profiles()}

    # ── Pre-warm missing peers in parallel (critical for Vercel cold starts) ──
    # On Vercel no eodhd_fund_*.json files are deployed, so _build_eodhd_multiples_index()
    # returns an empty index.  Without this block each peer lookup triggers a sequential
    # live EODHD API call (~12 × 3 s = 36 s), causing the request to time out.
    # Parallel pre-fetch caps the overhead to max(single_fetch_time) ≈ 3–5 s.
    _missing_from_index: list[str] = []
    for _tk in peer_tickers:
        _tk_upper = str(_tk or "").upper()
        if not any(eodhd_index.get(v) for v in _ticker_variants(_tk_upper)):
            _missing_from_index.append(_tk_upper)

    if _missing_from_index:
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

        def _prefetch_peer(ticker_text: str) -> tuple[str, dict | None]:
            raw = _fetch_eodhd_fundamentals_for_peer(ticker_text)
            return ticker_text, _metrics_from_eodhd_fundamentals(raw, fallback_ticker=ticker_text) if raw else None

        _max_workers = min(8, len(_missing_from_index))
        with ThreadPoolExecutor(max_workers=_max_workers) as _pool:
            for _result_tk, _metrics in _pool.map(_prefetch_peer, _missing_from_index):
                if _metrics:
                    _variants = _metrics.pop("_variants", set())
                    for _v in _variants:
                        if _v:
                            eodhd_index.setdefault(str(_v).upper(), _metrics)
        logger.debug(
            "fetch_peer_metrics: parallel pre-fetch completed %d missing peers for %s",
            len(_missing_from_index),
            target_ticker,
        )

    # yfinance fallback removed — see ADAPTIVE_DCF_IMPROVEMENT_PLAN.md (P3).
    # Tickers missing from the EODHD index are surfaced with N/A multiples.

    peers: list[dict] = []
    for tk in peer_tickers:
        try:
            ticker_text = str(tk or "").upper()

            # ── 1. EODHD cache (primary) ──────────────────────────────────
            eodhd: dict[str, Any] | None = _lookup_eodhd_peer_metrics(ticker_text, eodhd_index)

            if eodhd is not None:
                peers.append({
                    "ticker":     ticker_text,
                    "name":       eodhd.get("name") or ticker_text,
                    "market_cap": int(eodhd.get("market_cap") or 0),
                    "ev":         int(eodhd.get("ev") or 0),
                    "revenue":    None,
                    "ebitda":     None,
                    "ebit":       None,
                    "net_income": None,
                    "fcf":        None,
                    "ev_rev":     eodhd.get("ev_rev"),
                    "ev_ebitda":  eodhd.get("ev_ebitda"),
                    "ev_ebit":    eodhd.get("ev_ebit"),
                    "pe":         eodhd.get("pe"),
                    "p_fcf":      eodhd.get("p_fcf"),
                    "source":     eodhd.get("source") or "eodhd",
                    "exchange":   eodhd.get("exchange") or "",
                    "sector":     eodhd.get("sector") or "",
                    "industry":   eodhd.get("industry") or "",
                    "subject":    (ticker_text == target_ticker),
                })
                continue

            # ── 2. Not in EODHD index → mark as N/A (no yfinance fallback) ──
            peers.append({
                "ticker":     ticker_text,
                "name":       ticker_text,
                "market_cap": 0,
                "ev":         0,
                "revenue":    None, "ebitda": None, "ebit": None,
                "net_income": None, "fcf": None,
                "ev_rev":     None, "ev_ebitda": None, "ev_ebit": None,
                "pe":         None, "p_fcf": None,
                "subject":    (ticker_text == target_ticker),
                "source":     "not_available",
            })
        except Exception as exc:
            logger.debug("Peer fetch failed for %s: %s", tk, exc)
            peers.append({
                "ticker": str(tk or "").upper(), "name": str(tk or "").upper(), "market_cap": 0, "ev": 0,
                "revenue": None, "ebitda": None, "ebit": None,
                "net_income": None, "fcf": None,
                "ev_rev": None, "ev_ebitda": None, "ev_ebit": None,
                "pe": None, "p_fcf": None, "subject": (str(tk or "").upper() == target_ticker),
            })

    peers = _enrich_peer_rows(
        peers,
        target_ticker=target_ticker,
        peer_tickers=peer_tickers,
        target_sector=target_sector,
        target_industry=target_industry,
    )
    # Filter out invalid peers before caching and returning.
    # Subject row is always kept regardless of validity flag.
    valid_peers = [p for p in peers if p.get("subject") or p.get("peer_valid", True)]
    if len(valid_peers) < len(peers):
        excluded = [p["ticker"] for p in peers if not p.get("subject") and not p.get("peer_valid", True)]
        logger.info(
            "fetch_peer_metrics: excluded %d invalid peer(s) for %s: %s",
            len(peers) - len(valid_peers),
            target_ticker,
            excluded,
        )
    if any(_row_has_peer_multiple(peer) for peer in valid_peers):
        _save_cache(cache_key, valid_peers)

    peer_median = _compute_median(valid_peers, target_ticker)
    return valid_peers, peer_median


def _compute_median(peers: list[dict], target_ticker: str) -> dict:
    """Compute median multiples across non-subject peers with valid data."""
    def _med(key):
        vals = [p[key] for p in peers if not p.get("subject") and p.get(key) and p[key] > 0]
        if not vals:
            return None
        vals.sort()
        mid = len(vals) // 2
        return round(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2, 2)

    def _pct(key, pct):
        vals = sorted(p[key] for p in peers if not p.get("subject") and p.get(key) and p[key] > 0)
        if not vals:
            return None
        idx = max(0, int(len(vals) * pct / 100) - 1)
        return round(vals[idx], 2)

    return {
        "ev_rev":     _med("ev_rev"),
        "ev_ebitda":  _med("ev_ebitda"),
        "ev_ebit":    _med("ev_ebit"),
        "pe":         _med("pe"),
        "p_fcf":      _med("p_fcf"),
        "ev_rev_p25": _pct("ev_rev", 25),
        "ev_rev_p75": _pct("ev_rev", 75),
        "ev_ebitda_p25": _pct("ev_ebitda", 25),
        "ev_ebitda_p75": _pct("ev_ebitda", 75),
    }
