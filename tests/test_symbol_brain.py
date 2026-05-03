from __future__ import annotations

from auto_valuation.data.comps import build_cross_symbol_comps_view, compute_peer_set_stats
from auto_valuation.data.peers import rank_peer_candidates
from auto_valuation.learning.cross_industry import AnalogObservation, compute_global_overlay, find_analogs
from auto_valuation.learning.feature_space import FEATURE_NAMES, build_symbol_features


def _features(
    revenues: list[float],
    margins: list[float],
    *,
    sector: str,
    industry: str,
    market_cap: float,
    gross_margin: float = 60.0,
    capex_pct: float = 4.0,
    total_assets: float | None = None,
    total_debt: float = 40.0,
    operating_cf: float | None = None,
    fcf: float | None = None,
    da_pct: float = 2.0,
    tax_rate_pct: float = 21.0,
    observation_year: int = 2025,
):
    revenue_base = revenues[-1]
    return build_symbol_features(
        ticker=industry[:4].upper(),
        sector=sector,
        industry=industry,
        revenues=revenues,
        ebit_margins=margins,
        gross_margin_base_pct=gross_margin,
        capex_pct=capex_pct,
        total_assets=total_assets or max(revenue_base * 0.9, 1.0),
        total_debt=total_debt,
        revenue_base=revenue_base,
        operating_cf=operating_cf if operating_cf is not None else revenue_base * 0.22,
        fcf=fcf if fcf is not None else revenue_base * 0.16,
        da_pct=da_pct,
        tax_rate_pct=tax_rate_pct,
        market_cap=market_cap,
        observation_year=observation_year,
    )


def _analog(
    ticker: str,
    features,
    *,
    sector: str,
    industry: str,
    revenue_outcome: float = 0.0,
    margin_outcome_bps: float = 0.0,
    ev_outcome: float = 0.0,
    usefulness: float = 0.9,
    data_quality: float | None = None,
    as_of_year: int | None = None,
):
    return AnalogObservation(
        ticker=ticker,
        sector=sector,
        industry=industry,
        vintage_year=max(features.sample_size, 1),
        feature_map=dict(features.feature_map),
        outcome_revenue_cagr_5y=revenue_outcome,
        outcome_margin_change_bps=margin_outcome_bps,
        outcome_ev_multiple_change=ev_outcome,
        market_cap_regime=features.market_cap_regime,
        macro_regime=features.macro_regime,
        data_quality_score=data_quality if data_quality is not None else features.data_quality_score,
        sample_size=features.sample_size,
        predictive_usefulness=usefulness,
        as_of_year=as_of_year if as_of_year is not None else features.as_of_year,
    )


def test_feature_builder_handles_sparse_history():
    sparse = _features(
        [100.0, 112.0],
        [8.0, 9.5],
        sector="Industrials",
        industry="Tools",
        market_cap=1_200.0,
        gross_margin=34.0,
        capex_pct=5.5,
        total_assets=130.0,
        total_debt=35.0,
        operating_cf=18.0,
        fcf=11.0,
    )

    assert set(FEATURE_NAMES).issubset(sparse.feature_map)
    assert sparse.sample_size == 2
    assert sparse.data_quality_score < 0.8
    assert sparse.summary


def test_regime_aware_matching_changes_top_analog():
    scaling_subject = _features(
        [100, 130, 170, 220, 285, 360],
        [3, 5, 8, 11, 14, 17],
        sector="Technology",
        industry="Software",
        market_cap=12_000.0,
        gross_margin=68.0,
        capex_pct=3.0,
        total_assets=260.0,
        total_debt=25.0,
        operating_cf=70.0,
        fcf=40.0,
    )
    mature_subject = _features(
        [240, 252, 266, 279, 290, 300],
        [18, 19, 20, 21, 21.5, 22],
        sector="Technology",
        industry="Software",
        market_cap=12_000.0,
        gross_margin=72.0,
        capex_pct=2.5,
        total_assets=210.0,
        total_debt=20.0,
        operating_cf=90.0,
        fcf=72.0,
    )
    growth_peer = _analog(
        "GROW",
        _features(
            [95, 126, 162, 215, 280, 350],
            [4, 6, 8, 10, 13, 16],
            sector="Consumer Discretionary",
            industry="Internet Retail",
            market_cap=10_500.0,
            gross_margin=64.0,
            capex_pct=3.5,
            total_assets=255.0,
            total_debt=22.0,
            operating_cf=68.0,
            fcf=38.0,
        ),
        sector="Consumer Discretionary",
        industry="Internet Retail",
        revenue_outcome=0.18,
    )
    mature_peer = _analog(
        "MATURE",
        _features(
            [235, 248, 260, 274, 289, 301],
            [17.5, 18.6, 19.8, 20.5, 21.1, 21.8],
            sector="Industrials",
            industry="Services",
            market_cap=11_500.0,
            gross_margin=70.0,
            capex_pct=2.4,
            total_assets=215.0,
            total_debt=18.0,
            operating_cf=88.0,
            fcf=70.0,
        ),
        sector="Industrials",
        industry="Services",
        revenue_outcome=0.05,
    )

    scaling_matches = find_analogs(
        "SUBJ",
        scaling_subject,
        [growth_peer, mature_peer],
        subject_sector="Technology",
        subject_industry="Software",
        subject_vintage_year=scaling_subject.sample_size,
        subject_market_cap_regime=scaling_subject.market_cap_regime,
        cross_sector_only=False,
    )
    mature_matches = find_analogs(
        "SUBJ",
        mature_subject,
        [growth_peer, mature_peer],
        subject_sector="Technology",
        subject_industry="Software",
        subject_vintage_year=mature_subject.sample_size,
        subject_market_cap_regime=mature_subject.market_cap_regime,
        cross_sector_only=False,
    )

    assert scaling_matches.analogs[0].analog.ticker == "GROW"
    assert mature_matches.analogs[0].analog.ticker == "MATURE"


def test_analog_search_is_stable_under_small_feature_noise():
    subject = _features(
        [100, 122, 149, 182, 220, 268],
        [8, 10, 12, 13.5, 15, 16],
        sector="Technology",
        industry="Software",
        market_cap=8_500.0,
        gross_margin=66.0,
        capex_pct=3.2,
        total_assets=220.0,
        total_debt=18.0,
        operating_cf=55.0,
        fcf=39.0,
    )
    noisy_subject = _features(
        [100, 121, 151, 183, 223, 271],
        [8.2, 10.1, 12.1, 13.4, 15.1, 16.2],
        sector="Technology",
        industry="Software",
        market_cap=8_700.0,
        gross_margin=65.0,
        capex_pct=3.3,
        total_assets=224.0,
        total_debt=19.0,
        operating_cf=56.0,
        fcf=40.0,
    )
    top_peer = _analog(
        "TOP",
        _features(
            [98, 120, 146, 180, 218, 266],
            [7.8, 9.8, 11.8, 13.1, 14.8, 15.9],
            sector="Consumer Staples",
            industry="Brands",
            market_cap=8_200.0,
            gross_margin=64.0,
            capex_pct=3.4,
            total_assets=222.0,
            total_debt=20.0,
            operating_cf=54.0,
            fcf=38.0,
        ),
        sector="Consumer Staples",
        industry="Brands",
        revenue_outcome=0.10,
    )
    second_peer = _analog(
        "SECOND",
        _features(
            [100, 112, 124, 135, 145, 153],
            [10, 10.5, 11, 11.2, 11.4, 11.6],
            sector="Industrials",
            industry="Machinery",
            market_cap=5_100.0,
            gross_margin=41.0,
            capex_pct=7.5,
            total_assets=260.0,
            total_debt=60.0,
            operating_cf=26.0,
            fcf=15.0,
        ),
        sector="Industrials",
        industry="Machinery",
    )

    baseline = find_analogs("SUBJ", subject, [top_peer, second_peer], subject_sector="Technology", subject_industry="Software", subject_vintage_year=subject.sample_size, subject_market_cap_regime=subject.market_cap_regime, cross_sector_only=False)
    noisy = find_analogs("SUBJ", noisy_subject, [top_peer, second_peer], subject_sector="Technology", subject_industry="Software", subject_vintage_year=noisy_subject.sample_size, subject_market_cap_regime=noisy_subject.market_cap_regime, cross_sector_only=False)

    assert baseline.analogs[0].analog.ticker == "TOP"
    assert noisy.analogs[0].analog.ticker == "TOP"
    assert baseline.cohorts[0].label == noisy.cohorts[0].label


def test_cross_symbol_overlay_prefers_recent_useful_analogs():
    subject = _features(
        [120, 136, 154, 175, 198, 224],
        [11, 12, 13, 14, 15, 16],
        sector="Technology",
        industry="Software",
        market_cap=7_800.0,
        gross_margin=67.0,
        capex_pct=3.4,
        total_assets=205.0,
        total_debt=16.0,
        operating_cf=52.0,
        fcf=36.0,
        observation_year=2025,
    )
    recent_good = _analog(
        "RECENT",
        _features(
            [118, 135, 155, 176, 201, 229],
            [10.8, 12.1, 13.2, 14.2, 15.1, 16.1],
            sector="Consumer Discretionary",
            industry="Subscriptions",
            market_cap=8_100.0,
            gross_margin=66.0,
            capex_pct=3.3,
            total_assets=208.0,
            total_debt=17.0,
            operating_cf=53.0,
            fcf=37.0,
            observation_year=2024,
        ),
        sector="Consumer Discretionary",
        industry="Subscriptions",
        revenue_outcome=0.20,
        margin_outcome_bps=180.0,
        ev_outcome=1.8,
        usefulness=0.95,
        data_quality=0.95,
        as_of_year=2024,
    )
    stale_bad = _analog(
        "STALE",
        _features(
            [115, 133, 150, 172, 197, 221],
            [10.6, 11.8, 12.9, 13.8, 14.7, 15.5],
            sector="Industrials",
            industry="Services",
            market_cap=7_400.0,
            gross_margin=64.0,
            capex_pct=3.8,
            total_assets=210.0,
            total_debt=18.0,
            operating_cf=50.0,
            fcf=34.0,
            observation_year=2012,
        ),
        sector="Industrials",
        industry="Services",
        revenue_outcome=-0.30,
        margin_outcome_bps=-250.0,
        ev_outcome=-2.0,
        usefulness=0.30,
        data_quality=0.55,
        as_of_year=2012,
    )

    analogs = find_analogs(
        "SUBJ",
        subject,
        [recent_good, stale_bad],
        subject_sector="Technology",
        subject_industry="Software",
        subject_vintage_year=subject.sample_size,
        subject_market_cap_regime=subject.market_cap_regime,
        observation_year=2025,
        cross_sector_only=False,
    )
    overlay = compute_global_overlay(analogs)

    assert analogs.analogs[0].analog.ticker == "RECENT"
    assert overlay["enabled"] is True
    assert overlay["revenue_growth_adj_pp"] > 0
    assert overlay["ebit_margin_adj_pp"] > 0


def test_peer_and_comps_views_use_analog_weights():
    subject = _features(
        [140, 160, 182, 205, 232, 260],
        [12, 13, 14, 15, 16, 17],
        sector="Technology",
        industry="Software",
        market_cap=9_600.0,
        gross_margin=65.0,
        capex_pct=3.5,
        total_assets=240.0,
        total_debt=22.0,
        operating_cf=60.0,
        fcf=41.0,
    )
    peer_a = _analog(
        "PEERA",
        _features(
            [138, 158, 180, 204, 233, 263],
            [12, 13, 14, 15.1, 16.1, 17.1],
            sector="Consumer Discretionary",
            industry="Subscriptions",
            market_cap=9_800.0,
            gross_margin=64.0,
            capex_pct=3.6,
            total_assets=238.0,
            total_debt=20.0,
            operating_cf=61.0,
            fcf=42.0,
        ),
        sector="Consumer Discretionary",
        industry="Subscriptions",
        revenue_outcome=0.12,
    )
    peer_b = _analog(
        "PEERB",
        _features(
            [125, 136, 148, 160, 171, 183],
            [10.5, 11, 11.5, 12, 12.5, 13],
            sector="Industrials",
            industry="Services",
            market_cap=5_200.0,
            gross_margin=42.0,
            capex_pct=7.2,
            total_assets=255.0,
            total_debt=45.0,
            operating_cf=29.0,
            fcf=18.0,
        ),
        sector="Industrials",
        industry="Services",
    )
    peer_c = _analog(
        "PEERC",
        _features(
            [142, 156, 173, 191, 210, 231],
            [11.5, 12.2, 13, 13.8, 14.3, 14.8],
            sector="Health Care",
            industry="Devices",
            market_cap=8_300.0,
            gross_margin=58.0,
            capex_pct=4.5,
            total_assets=246.0,
            total_debt=24.0,
            operating_cf=55.0,
            fcf=35.0,
        ),
        sector="Health Care",
        industry="Devices",
    )

    ranking = rank_peer_candidates(
        "SUBJ",
        subject,
        [peer_a, peer_b, peer_c],
        subject_sector="Technology",
        subject_industry="Software",
        subject_vintage_year=subject.sample_size,
        subject_market_cap_regime=subject.market_cap_regime,
        cross_sector_only=False,
    )
    peer_rows = [
        {"ticker": "PEERA", "ev_revenue_ltm": 9.5, "ev_ebitda_ltm": 26.0},
        {"ticker": "PEERB", "ev_revenue_ltm": 4.0, "ev_ebitda_ltm": 11.0},
        {"ticker": "PEERC", "ev_revenue_ltm": 6.5, "ev_ebitda_ltm": 18.0},
    ]
    simple = compute_peer_set_stats(peer_rows)
    weighted = build_cross_symbol_comps_view(peer_rows, ranking["peers"])

    assert ranking["peers"][0]["ticker"] == "PEERA"
    assert weighted["top_ranked_peers"][0]["ticker"] == "PEERA"
    assert weighted["weighted_stats"]["ev_revenue_ltm"]["weighted_mean"] > simple["ev_revenue_ltm"]["mean"]