"""
webapp/data/peer_lists.py
─────────────────────────
Industry peer-group definitions and live metric fetching via yfinance.

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
import time
from typing import Any

from auto_valuation.learning.industry_taxonomy import industry_similarity, related_industries, resolve_industry_taxonomy

logger = logging.getLogger(__name__)

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
        "Autonomy / Software":    ["MOBILEYE", "UBER", "GOOGL"],
    },
}

# Industry-level peer lists (yfinance industry string → peer tickers)
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
    "Consumer Electronics":       ["AAPL", "MSFT", "GOOGL", "META", "SONY", "HPQ"],
    "Internet Content & Information": ["META", "GOOGL", "SNAP", "PINS", "RDDT", "TWTR"],
    "Electronic Components":      ["HON", "TE", "APTV", "FLEX", "JBL"],
    "Information Technology Services": ["INFY", "WIT", "ACN", "IBM", "CTSH", "TCS"],
    "Computer Hardware":          ["AAPL", "HPQ", "DELL", "HPE", "NTAP", "PSTG"],
    # Healthcare
    "Drug Manufacturers—General": ["JNJ", "PFE", "MRK", "ABBV", "BMY", "LLY", "NVO", "ROCHE"],
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
    "Oil & Gas—Integrated":       ["XOM", "CVX", "SHEL", "BP", "TTE", "ENI"],
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

_YFINANCE_EXCHANGE_SUFFIX = {
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


def _to_yfinance_ticker(code: str, exchange: str) -> str | None:
    symbol = (code or "").upper().strip()
    venue = (exchange or "").upper().strip()
    if not symbol:
        return None
    if "." in symbol:
        return symbol
    suffix = _YFINANCE_EXCHANGE_SUFFIX.get(venue)
    if suffix is None:
        return symbol if venue in {"", "OTC", "PINK"} else None
    return f"{symbol}{suffix}"


@lru_cache(maxsize=1)
def _load_cached_peer_profiles() -> tuple[dict[str, Any], ...]:
    cache_dir = Path(__file__).with_name("cache")
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
        ticker = _to_yfinance_ticker(str(code), str(exchange))
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
    return tuple(profiles)


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
            or (
                include_related
                and subject_taxonomy.get("family")
                and subject_taxonomy.get("family") == candidate_taxonomy.get("family")
            )
        )
        if not same_canonical and (not include_related or not related_match or similarity < 0.45):
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
        profile = profiles.get(candidate)
        candidate_sector = str((profile or {}).get("sector") or "")
        candidate_industry = str((profile or {}).get("industry") or "")
        similarity = industry_similarity(
            industry,
            candidate_industry,
            subject_sector=sector,
            candidate_sector=candidate_sector,
        )
        score = similarity * 60.0

        candidate_exchange = str((profile or {}).get("exchange") or "")
        if subject_exchange and candidate_exchange and candidate_exchange == subject_exchange:
            score += 4.0

        candidate_market_cap = float((profile or {}).get("market_cap_mln") or 0.0)
        if subject_market_cap > 0 and candidate_market_cap > 0:
            market_cap_gap = abs(math.log10(max(candidate_market_cap, 1.0) / max(subject_market_cap, 1.0)))
            score += max(0.0, 8.0 - market_cap_gap * 8.0)

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

    Priority: multi-segment override → same-industry cached peers + industry map
    → sector fallback.
    Self-ticker is always removed.  Max 12 unique peers returned.
    """
    ticker = ticker.upper()

    # 1. Multi-segment override
    if ticker in MULTI_SEGMENT_PEERS:
        seen: set[str] = set()
        peers: list[str] = []
        for seg_peers in MULTI_SEGMENT_PEERS[ticker].values():
            for p in seg_peers:
                if p not in seen and p != ticker:
                    seen.add(p)
                    peers.append(p)
        return peers[:12]

    # 2. Cached same-industry discovery + curated industry map.
    cached_industry_peers = _discover_cached_peers(ticker, sector, industry, max_peers=12, include_related=False)
    curated_industry_peers = _industry_peers(industry, include_related=False)
    industry_peers = _dedupe_preserve(
        cached_industry_peers + curated_industry_peers,
        exclude=_ticker_variants(ticker),
        limit=10,
    )
    if len(industry_peers) >= 6:
        return _rank_peer_tickers(industry_peers, subject_ticker=ticker, sector=sector, industry=industry)

    related_cached_peers = _discover_cached_peers(ticker, sector, industry, max_peers=12, include_related=True)
    related_curated_peers = _industry_peers(industry, include_related=True)
    industry_peers = _dedupe_preserve(
        industry_peers + related_cached_peers + related_curated_peers,
        exclude=_ticker_variants(ticker),
        limit=10,
    )
    if industry_peers:
        return _rank_peer_tickers(industry_peers, subject_ticker=ticker, sector=sector, industry=industry)

    # 3. Sector fallback.
    sector_peers = _dedupe_preserve(_sector_peers(sector), exclude=_ticker_variants(ticker), limit=8)
    if sector_peers:
        return _rank_peer_tickers(sector_peers, subject_ticker=ticker, sector=sector, industry=industry)

    # 4. Generic tech fallback for any unrecognised ticker
    return ["AAPL", "MSFT", "GOOGL", "META", "NVDA", "ORCL", "CRM"]


def get_segment_peers(ticker: str) -> dict[str, list[str]]:
    """Return segment-level peer breakdown if available, else {}."""
    return MULTI_SEGMENT_PEERS.get(ticker.upper(), {})


# ─── Live metric fetching ─────────────────────────────────────────────────────

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
_CACHE_TTL  = 86_400  # 24 hours


def _cache_path(ticker: str) -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, f"{ticker.upper()}_peers.json")


def _load_cache(ticker: str) -> list | None:
    p = _cache_path(ticker)
    if not os.path.exists(p):
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
    try:
        with open(_cache_path(ticker), "w") as f:
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


def fetch_peer_metrics(
    peer_tickers: list[str],
    target_ticker: str,
    timeout_per_peer: float = 6.0,
    target_sector: str = "",
    target_industry: str = "",
) -> tuple[list[dict], dict]:
    """Fetch basic valuation metrics for *peer_tickers* via yfinance.

    Returns:
        peers       — list of peer dicts (one per ticker, sorted by mkt cap desc)
        peer_median — dict of median multiples across the peer group
    """
    try:
        import yfinance as yf
    except ImportError:
        return [], {}

    target_ticker = target_ticker.upper()
    cache_key = _peer_cache_key(target_ticker, peer_tickers)

    cached = _load_cache(cache_key)
    if cached is not None:
        logger.debug("Using cached peer data for %s", cache_key)
        peers = _enrich_peer_rows(
            list(cached),
            target_ticker=target_ticker,
            peer_tickers=peer_tickers,
            target_sector=target_sector,
            target_industry=target_industry,
        )
        peer_median = _compute_median(peers, target_ticker)
        return peers, peer_median

    peers: list[dict] = []
    for tk in peer_tickers:
        try:
            info = yf.Ticker(tk).info or {}
            mkt = _safe_float(info.get("marketCap", 0)) / 1e6
            rev = _safe_float(info.get("totalRevenue", 0)) / 1e6
            ebitda = _safe_float(info.get("ebitda", 0)) / 1e6
            ebit   = _safe_float(info.get("ebit", 0)) / 1e6
            ni     = _safe_float(info.get("netIncomeToCommon", 0)) / 1e6
            fcf    = _safe_float(info.get("freeCashflow", 0)) / 1e6
            td     = _safe_float(info.get("totalDebt", 0)) / 1e6
            cash   = _safe_float(info.get("totalCash", 0)) / 1e6
            ev     = mkt + td - cash if mkt > 0 else 0

            def _mult(num, den):
                if den and den > 0 and num and num > 0:
                    return round(num / den, 2)
                return None

            peers.append({
                "ticker":    tk.upper(),
                "name":      info.get("shortName") or info.get("longName") or tk,
                "market_cap": round(mkt),
                "ev":         round(ev),
                "revenue":    round(rev),
                "ebitda":     round(ebitda) if ebitda > 0 else None,
                "ebit":       round(ebit)   if ebit != 0 else None,
                "net_income": round(ni)     if ni != 0 else None,
                "fcf":        round(fcf)    if fcf != 0 else None,
                "ev_rev":     _mult(ev, rev),
                "ev_ebitda":  _mult(ev, ebitda),
                "ev_ebit":    _mult(ev, ebit),
                "pe":         _mult(mkt, ni),
                "p_fcf":      _mult(mkt, fcf),
                "subject":    (tk.upper() == target_ticker),
            })
        except Exception as exc:
            logger.debug("Peer fetch failed for %s: %s", tk, exc)
            peers.append({
                "ticker": tk.upper(), "name": tk, "market_cap": 0, "ev": 0,
                "revenue": None, "ebitda": None, "ebit": None,
                "net_income": None, "fcf": None,
                "ev_rev": None, "ev_ebitda": None, "ev_ebit": None,
                "pe": None, "p_fcf": None, "subject": (tk.upper() == target_ticker),
            })

    peers = _enrich_peer_rows(
        peers,
        target_ticker=target_ticker,
        peer_tickers=peer_tickers,
        target_sector=target_sector,
        target_industry=target_industry,
    )
    _save_cache(cache_key, peers)

    peer_median = _compute_median(peers, target_ticker)
    return peers, peer_median


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
