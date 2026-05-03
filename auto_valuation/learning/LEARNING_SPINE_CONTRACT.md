# Learning Spine Contract

This contract is the stable storage and query surface for time-safe learning across all symbols.

## Storage

Predictions stay in `prediction_records`.

Additive prediction metadata now includes:
- `prediction_timestamp`
- `horizon_target_date`
- `horizon_label`
- `horizon_months`
- `fiscal_year_end_month`
- `fiscal_year_end_day`
- `prediction_context_json`

Realized outcomes are append-only in `realized_outcomes`.
Each row is one aligned evidence snapshot for one prediction record.
Required context fields:
- `record_id`
- `ticker`
- `forecast_horizon_year`
- `horizon_target_date`
- `horizon_label`
- `label_as_of_date`
- `aligned_period_end`
- `alignment_method`
- `source_name`
- `source_kind`
- `label_status`
- `known_target_count`
- `unknown_targets_json`
- `source_payload_json`

Maintenance runs are stored in `maintenance_runs` with append-only payload history.

## Alignment Rules

Use `prediction_horizon_target_date(prediction)` as the canonical target date.
For fundamentals backfill, `align_prediction_record_to_actuals(...)` only matches when:
- the prediction horizon target date is not in the future relative to the maintenance run
- the source period end is not in the future relative to the maintenance run
- `aligned_period_end == horizon_target_date` when strict alignment is enabled

Do not infer missing targets.
If a source only provides some realized values, store the known values and list the missing targets in `unknown_targets`.

## Query API

Use `LedgerReader.query_aligned_pairs(...)` for global learning pairs.
Recommended defaults:
- `scenario="base"`
- `matured_only=True`
- `require_realized=True`
- `include_partial=True`

Useful companion APIs:
- `LedgerReader.query_realized_outcomes(...)`
- `LedgerReader.get_best_realized_outcome(record_id, ...)`
- `LedgerReader.query_maintenance_runs(...)`

`query_aligned_pairs(...)` returns one preferred realized snapshot per prediction, chosen by:
- highest known target count
- complete labels over partial labels
- latest `label_as_of_date`
- latest append order

## Backward Compatibility

Legacy summary fields on `prediction_records` are still filled when they are currently null.
Existing postmortem flows still work, but annual postmortems now prefer aligned realized evidence already stored in the spine.
Existing rows are not destructively rewritten.
