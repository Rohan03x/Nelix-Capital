# Symbol Brain Handoff

## Time-Safe Contract

All feature construction is forecast-time safe.

- `build_symbol_features(...)` only uses trailing financial history and point-in-time market context supplied by the caller.
- `build_analog_observations(records)` only uses stored prediction-time feature vectors from the ledger and treats realized outcomes as labels for transfer learning.
- `find_analogs(...)` compares symbol states, not future outcomes.

## Primary APIs

- `auto_valuation.learning.build_symbol_features(...) -> SymbolFeatures`
  - Shared multi-symbol representation for a company state.
  - Includes `feature_map`, `feature_vector`, `dimensions`, `summary`, `maturity_stage`, `valuation_regime`, `volatility_regime`, `market_cap_regime`, `data_quality_score`, and `sample_size`.

- `auto_valuation.learning.build_feature_map(...) -> dict[str, float]`
  - Lightweight wrapper when only the numeric feature map is needed.

- `auto_valuation.learning.build_analog_observations(records) -> list[AnalogObservation]`
  - Converts matured ledger records into reusable analog candidates with recency, data-quality, sample-size, and predictive-usefulness metadata.

- `auto_valuation.learning.find_analogs(...) -> AnalogSet`
  - Returns nearest analogs, weighted outcomes, cohort clustering, pattern match, and the computed overlay.

- `auto_valuation.learning.form_cohorts(...) -> list[AnalogCohort]`
  - Groups analogs into explainable cohorts using maturity, valuation regime, and volatility regime.

- `auto_valuation.learning.compute_global_overlay(analog_set) -> dict[str, Any]`
  - Produces cross-symbol transfer adjustments and overlay metadata.

- `auto_valuation.data.peers.rank_peer_candidates(...) -> dict[str, Any]`
  - Peer selection entry point for analog-aware peer ranking.

- `auto_valuation.data.comps.compute_weighted_peer_set_stats(...) -> dict[str, dict[str, Any]]`
  - Weighted comps statistics keyed by analog score.

- `auto_valuation.data.comps.build_cross_symbol_comps_view(...) -> dict[str, Any]`
  - Bundles weighted comps stats with top-ranked peer evidence.

## Knowledge-Model Payload

`webapp.data.knowledge_model.refine_live_assumptions(...)` now returns two new UI/calibration-facing sections.

- `knowledge_model.symbol_brain`
  - `summary: str`
  - `feature_map: dict[str, float]`
  - `feature_vector: list[float]`
  - `dimensions: list[{name, label, group, value, weight, display_value, bucket, evidence}]`
  - `maturity_stage: str`
  - `valuation_regime: str`
  - `volatility_regime: str`
  - `market_cap_regime: str`
  - `data_quality_score: float`
  - `sample_size: int`

- `knowledge_model.analogs`
  - `enabled: bool`
  - `count: int`
  - `pattern_match: str | None`
  - `pattern_match_score: float`
  - `cohorts: list[{label, score, members, explanation}]`
  - `items: list[...]`
  - `overlay: dict[str, Any]`

## Analog Item Shape

Each entry in `knowledge_model.analogs.items` has:

- `ticker: str`
- `sector: str`
- `industry: str`
- `score: float`
- `similarity: float`
- `static_similarity: float`
- `regime_similarity: float`
- `weights: {recency, data_quality, sample, usefulness}`
- `maturity_stage: str`
- `valuation_regime: str`
- `volatility_regime: str`
- `evidence: list[{dimension, label, similarity, subject, analog, bucket}]`

## Overlay Shape

`knowledge_model.analogs.overlay` and `compute_global_overlay(...)` return:

- `enabled: bool`
- `scope: str | None`
- `confidence: float`
- `analog_count: int`
- `cohort_size: int`
- `sector_span: int`
- `revenue_growth_adj_pp: float`
- `ebit_margin_adj_pp: float`
- `valuation_multiple_adj: float`
- `wacc_adj_pp: float`
- `terminal_growth_adj_pp: float`
- `beta_adj: float`
- `top_analogs: list[{ticker, score, similarity, maturity_stage, valuation_regime}]`
- `note: str | None`

## Recommended Usage

- Calibration agents should use `build_symbol_features(...)` and `find_analogs(...)` instead of constructing ad hoc peer vectors.
- UI agents should render `symbol_brain.summary`, `analogs.items[*].evidence`, and `analogs.cohorts` to explain why an analog or cluster was chosen.
- Comps agents should pass `rank_peer_candidates(...)["peers"]` into `build_cross_symbol_comps_view(...)` for analog-weighted comparable stats.