# Layered Learning Output Contract

The live dashboard payload now exposes a layered learning contract under `knowledge_model`.

UI render contract:
- Render `knowledge_model.summary` as the top-line explanation.
- Render `knowledge_model.layered_learning.layer_mix` as the six memory layers: `company_memory`, `sector_memory`, `cohort_memory`, `analog_memory`, `macro_memory`, `global_memory`.
- For each layer, show `weight_pct`, `records`, `enabled`, and `note`.
- Render `knowledge_model.layered_learning.structural_break` with `detected`, `score`, `reasons`, and `note`.
- Render `knowledge_model.layered_learning.uncertainty` with `effective_confidence`, `scenario_width_multiplier`, `weak_evidence`, `conflict_score`, and `note`.
- Render `knowledge_model.layered_learning.learned_metrics` with `ufcf_margin_pct`, `ufcf_margin_adjustment_pp`, `reinvestment_rate_pct`, `reinvestment_adjustment_pp`, confidence fields, and the optional implied capex fields.
- Keep rendering `knowledge_model.explainability` for the dashboard panel. It now includes `sector_memory`, `analog_memory`, `macro_memory`, `global_memory`, `structural_break`, `uncertainty`, and `learned_metrics` in addition to the existing sections.

Live inference contract:
- Use `knowledge_model.scenario_width_multiplier` to widen bull/base/bear spreads. The dashboard client now consumes this directly.
- Prefer `knowledge_model.learning_confidence` for the layered engine's effective confidence, and keep `knowledge_model.calibration_confidence` as the raw cohort-calibrator confidence for backward compatibility.
- Use `knowledge_model.capex_pct` directly; it now includes a small learned reinvestment proxy blend when realized cashflow evidence exists.
- Treat `knowledge_model.layered_learning.structural_break.detected` as a caution flag, not a hard mode switch.

Backward compatibility:
- Existing fields like `global_learning`, `assumption_weights`, `summary`, `calibration_cohort_size`, and `explainability` remain present.
- The health check now expects `knowledge_model.layered_learning` to exist.