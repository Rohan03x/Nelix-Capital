"""
assumptions/revenue.py — Historical revenue growth decomposition.

Decomposes observed revenue changes into price, volume, and (optionally) mix
components to inform the forward price/volume/mix bridge in forecast.py.

Reference: Architecture Plan Part 57.
"""

from __future__ import annotations


def decompose_historical_revenue_growth(
    revenues_hist: list[float],
    prices_hist: list[float] | None = None,
) -> tuple[list[float | None], list[float | None] | None, list[float | None] | None]:
    """
    Decompose observed historical revenue into blended, price, and volume growth rates.

    Formula (multiplicative):
        blended_g_t = (Revenue_t / Revenue_{t-1}) - 1
        price_g_t   = (Price_t   / Price_{t-1})   - 1
        volume_g_t  = (Volume_t  / Volume_{t-1})  - 1
    where Volume_t = Revenue_t / Price_t.

    If *prices_hist* is not provided (None), only blended growth is returned.

    Periods where the prior-year value is zero or None are stored as None
    to avoid division-by-zero silently altering results.

    Args:
        revenues_hist: Time-ordered list of historical revenue values ($M).
                       Must have at least 2 elements to produce any growth rate.
        prices_hist:   Optional parallel list of price index or average unit-price
                       values. Must be the same length as revenues_hist.

    Returns:
        (blended_growths, price_growths, volume_growths)
          - blended_growths: len = len(revenues_hist) - 1
          - price_growths:   len = len(revenues_hist) - 1, or None if prices_hist absent
          - volume_growths:  len = len(revenues_hist) - 1, or None if prices_hist absent

    Reference: Architecture Plan Part 57.
    """
    n = len(revenues_hist)

    # ── Blended growth ──────────────────────────────────────────────────────
    blended_growths: list[float | None] = []
    for i in range(1, n):
        prev = revenues_hist[i - 1]
        curr = revenues_hist[i]
        if prev and prev > 0:
            blended_growths.append((curr - prev) / prev)
        else:
            blended_growths.append(None)

    # ── Price / volume decomposition (only if price index provided) ─────────
    if prices_hist is None or len(prices_hist) != n:
        return blended_growths, None, None

    # Derive implied volume indices: Volume_t = Revenue_t / Price_t
    volumes: list[float | None] = []
    for r, p in zip(revenues_hist, prices_hist):
        if p and p > 0:
            volumes.append(r / p)
        else:
            volumes.append(None)

    price_growths:  list[float | None] = []
    volume_growths: list[float | None] = []

    for i in range(1, n):
        p_prev = prices_hist[i - 1]
        p_curr = prices_hist[i]
        v_prev = volumes[i - 1]
        v_curr = volumes[i]

        if p_prev and p_prev > 0:
            price_growths.append((p_curr - p_prev) / p_prev)
        else:
            price_growths.append(None)

        if v_prev and v_prev > 0 and v_curr is not None:
            volume_growths.append((v_curr - v_prev) / v_prev)
        else:
            volume_growths.append(None)

    return blended_growths, price_growths, volume_growths
