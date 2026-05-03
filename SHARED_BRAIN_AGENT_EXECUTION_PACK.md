# Shared-Brain Agent Execution Pack

This pack turns the current shared-brain follow-up work into five non-overlapping agent tracks with explicit file ownership, merge order, and success gates.

## Current State

- The packaged benchmark is now at provisional status, not gap.
- Latest packaged benchmark result from `validate_shared_brain.py --no-diagnostics`:
  - revenue growth MAE: `0.6875 -> 0.6250`
  - EBIT margin MAE: `0.1978 -> 0.1890`
  - UFCF error pct MAE: `1.7744 -> 1.7068`
  - valuation error pct MAE: `10.8950 -> 10.4527`
  - confidence ranking accuracy: `66.67%`
  - analog consistency rate: `100.00%`
  - acceptance: `provisional`
- Full acceptance is still blocked by thin live evidence.
- Latest diagnostic state from `validate_shared_brain.py` and `check.py --strict-learning` shows a live ledger with predictions recorded, but no matured postmortems or quinquennial reports yet.

## Global Rules

- Read-any, write-owned-only.
- Each file has one writing owner for this execution pack.
- Cross-cutting integration files are reserved for the Acceptance and Operations Agent.
- Other agents should expose additive modules, helper functions, and focused tests instead of editing reserved files.
- If an agent needs a new interface, it should create a new owned module and document the import contract for the Acceptance and Operations Agent to wire in.
- Each agent should land its own focused tests. Only the Acceptance and Operations Agent edits the umbrella benchmark and repo-facing validation surfaces.

## Reserved Cross-Cutting Files

Only the Acceptance and Operations Agent may edit these files:

- `webapp/data/knowledge_model.py`
- `auto_valuation/validation/shared_brain.py`
- `auto_valuation/validation/__init__.py`
- `validate_shared_brain.py`
- `check.py`
- `auto_valuation/learning/__init__.py`
- `tests/test_learning_system.py`
- `README.md`
- `CHANGELOG.md`
- `SHARED_BRAIN_VALIDATION.md`

## Recommended Merge Order

- Wave 1 in parallel: Dynamic Relationship Graph Agent, Live Evidence Bootstrap Agent
- Wave 2: Revenue Growth Regime Agent after the graph agent publishes any new reusable state or edge contract
- Wave 3: Confidence Ranking Agent after the bootstrap agent publishes richer evidence and freshness signals
- Wave 4: Acceptance and Operations Agent last, for final wiring, validator hardening, docs, and end-state status

## Agent 1: Revenue Growth Regime Agent

### Write-Owned Files

- `auto_valuation/learning/revenue_regime.py` (new)
- `auto_valuation/learning/calibrator.py`
- `auto_valuation/learning/_layered_calibrator.py`
- `tests/test_revenue_regime.py` (new)
- `auto_valuation/learning/GROWTH_REGIME_HANDOFF.md` (new)

### Read-Only Context

- `webapp/data/knowledge_model.py`
- `auto_valuation/learning/feature_space.py`
- `auto_valuation/learning/cross_industry.py`
- `auto_valuation/validation/shared_brain.py`
- `tests/test_learning_system.py`
- `SHARED_BRAIN_VALIDATION.md`

### Must Not Edit

- Any reserved cross-cutting file
- `webapp/data/eodhd_client.py`
- `auto_valuation/learning/ledger.py`
- `auto_valuation/learning/maintenance.py`
- `auto_valuation/learning/postmortem.py`

### Prompt

> You are the Revenue Growth Regime Agent for this repository. Your job is to harden the most fragile packaged-benchmark metric: revenue-growth accuracy. The packaged shared-brain benchmark now beats baseline on revenue growth, but only narrowly, so the goal is to make that improvement more robust without giving back the margin, UFCF, or valuation gains that already exist.
>
> Start from: `auto_valuation/learning/calibrator.py`, `auto_valuation/learning/_layered_calibrator.py`, `auto_valuation/learning/feature_space.py`, `auto_valuation/learning/cross_industry.py`, `auto_valuation/validation/shared_brain.py`, `tests/test_learning_system.py`.
>
> Write only to: `auto_valuation/learning/revenue_regime.py`, `auto_valuation/learning/calibrator.py`, `auto_valuation/learning/_layered_calibrator.py`, `tests/test_revenue_regime.py`, and `auto_valuation/learning/GROWTH_REGIME_HANDOFF.md`. Do not edit `webapp/data/knowledge_model.py`, `auto_valuation/validation/shared_brain.py`, or any repo-facing docs. If you need a new integration seam, expose it from your owned files and document the contract in your handoff doc.
>
> Mission: build a regime-aware, sequence-aware revenue-growth learner that understands deceleration, re-acceleration, saturation, cyclic recovery, margin-growth tradeoffs, reinvestment intensity, and base-effect distortion. It must remain time-safe and explainable.
>
> Must do: add growth-state logic and regime tags through owned helper modules; learn revenue-growth corrections from aligned multi-symbol outcome pairs; use analog sequences rather than only static residual averages; apply shrinkage so weak evidence falls back toward baseline; add guardrails so the growth override only activates when expected improvement is strong enough; emit an explanation object that the integration layer can surface later.
>
> Constraints: no future leakage, no opaque black-box-only correction, no blind mean reversion, and no material valuation-error degradation just to win the growth metric.
>
> Deliverables: new or extended growth-learning module, focused tests, and a handoff doc with the exact import and payload contract for the Acceptance and Operations Agent.
>
> Success condition: your focused tests pass, and when the Acceptance and Operations Agent wires your module in, the packaged benchmark keeps revenue-growth MAE at or below baseline without materially worsening valuation error.

### Focused Validation

- `python -m pytest tests/test_revenue_regime.py -q`

## Agent 2: Live Evidence Bootstrap Agent

### Write-Owned Files

- `auto_valuation/learning/ledger.py`
- `auto_valuation/learning/maintenance.py`
- `auto_valuation/learning/postmortem.py`
- `auto_valuation/learning/bootstrap_history.py` (new)
- `webapp/data/eodhd_client.py`
- `tests/test_learning_spine.py`
- `tests/test_learning_bootstrap.py` (new)
- `auto_valuation/learning/LEARNING_SPINE_CONTRACT.md`

### Read-Only Context

- `auto_valuation/validation/shared_brain.py`
- `validate_shared_brain.py`
- `check.py`
- `SHARED_BRAIN_VALIDATION.md`

### Must Not Edit

- Any reserved cross-cutting file
- `webapp/data/knowledge_model.py`
- `auto_valuation/learning/confidence.py`
- `auto_valuation/learning/feature_space.py`
- `auto_valuation/learning/cross_industry.py`

### Prompt

> You are the Live Evidence Bootstrap Agent for this repository. Your job is to turn the shared-brain system from wired-up into historically evidenced by generating aligned realized outcomes, postmortems, and quinquennial reports from matured predictions.
>
> Start from: `auto_valuation/learning/ledger.py`, `auto_valuation/learning/maintenance.py`, `auto_valuation/learning/postmortem.py`, `webapp/data/eodhd_client.py`, `auto_valuation/validation/shared_brain.py`, `tests/test_learning_spine.py`.
>
> Write only to: `auto_valuation/learning/ledger.py`, `auto_valuation/learning/maintenance.py`, `auto_valuation/learning/postmortem.py`, `auto_valuation/learning/bootstrap_history.py`, `webapp/data/eodhd_client.py`, `tests/test_learning_spine.py`, `tests/test_learning_bootstrap.py`, and `auto_valuation/learning/LEARNING_SPINE_CONTRACT.md`. Do not edit validator files or shared integration files.
>
> Current gap: live diagnostics still show a thin ledger with no matured postmortems or quinquennial reports. That blocks a credible live acceptance claim even though the packaged benchmark now passes provisionally.
>
> Mission: build a historical bootstrap and replay job that scans the prediction ledger, finds matured horizons, aligns available fundamentals and market-price evidence to each prediction, writes append-only realized outcomes, and materializes postmortems and quinquennial summaries where eligible.
>
> Must do: support partial and full labels; align strictly by target period end and label-as-of date; capture realized price-at-horizon and EV-at-horizon where feasible; preserve source metadata; make the bootstrap idempotent; expose a repeatable CLI or maintenance entry point; log unlabeled cases instead of inventing values.
>
> Constraints: never rewrite history destructively, never duplicate outcome rows, never infer unavailable realized labels, and never break the existing live maintenance path.
>
> Deliverables: bootstrap command or module, operational tests, updated spine contract doc, and a handoff note describing how diagnostics should reflect new evidence coverage.
>
> Success condition: your focused tests pass, and once the Acceptance and Operations Agent runs the full validator, diagnostics show materially populated realized outcomes and postmortems with no duplicate or misaligned rows.

### Focused Validation

- `python -m pytest tests/test_learning_spine.py tests/test_learning_bootstrap.py -q`

## Agent 3: Confidence Ranking Agent

### Write-Owned Files

- `auto_valuation/learning/confidence.py`
- `auto_valuation/learning/confidence_ranking.py` (new)
- `webapp/templates/dashboard.html`
- `tests/test_webapp_audit.py`
- `tests/test_confidence_ranking.py` (new)
- `auto_valuation/learning/CONFIDENCE_HANDOFF.md` (new)

### Read-Only Context

- `webapp/data/knowledge_model.py`
- `auto_valuation/validation/shared_brain.py`
- `check.py`
- `tests/test_learning_system.py`
- `SHARED_BRAIN_VALIDATION.md`

### Must Not Edit

- Any reserved cross-cutting file
- `auto_valuation/learning/ledger.py`
- `auto_valuation/learning/maintenance.py`
- `auto_valuation/learning/postmortem.py`
- `auto_valuation/learning/feature_space.py`
- `auto_valuation/learning/cross_industry.py`

### Prompt

> You are the Confidence Ranking Agent for this repository. Your job is to make the confidence score rank future forecast quality, not merely describe it.
>
> Start from: `auto_valuation/learning/confidence.py`, `webapp/data/knowledge_model.py`, `webapp/templates/dashboard.html`, `auto_valuation/validation/shared_brain.py`, `tests/test_webapp_audit.py`, `tests/test_learning_system.py`.
>
> Write only to: `auto_valuation/learning/confidence.py`, `auto_valuation/learning/confidence_ranking.py`, `webapp/templates/dashboard.html`, `tests/test_webapp_audit.py`, `tests/test_confidence_ranking.py`, and `auto_valuation/learning/CONFIDENCE_HANDOFF.md`. Do not edit `webapp/data/knowledge_model.py`; instead, expose a clear helper and payload contract for the Acceptance and Operations Agent to wire in.
>
> Current gap: packaged confidence ranking accuracy is now `66.67%`, which is better than before but still too weak to treat the score as a strong decision aid.
>
> Mission: rebuild confidence as a calibrated error-ranking system driven by realized historical outcomes. Confidence should explicitly depend on evidence count, analog dispersion, layer conflict, scenario width, structural-break probability, maintenance freshness, data quality, similarity strength, and valuation sensitivity.
>
> Must do: create a monotonic calibration layer between historical evidence and expected error bands; separate confidence in assumptions from confidence in valuation; punish disagreement across company, sector, cohort, analog, and global layers; reduce confidence when analog evidence is thin or unstable; expose a dashboard-ready decomposition that is actually understandable.
>
> Constraints: do not inflate confidence to look better, do not hide weak evidence, and do not make the dashboard noisier than the insight it provides.
>
> Deliverables: improved confidence engine, focused UI and ranking tests, and a handoff doc with the exact helper and payload contract for later integration.
>
> Success condition: your focused tests pass, and when the Acceptance and Operations Agent wires the new confidence layer in, confidence ranking accuracy rises materially above the current `66.67%` while low-confidence cases continue to correspond to higher realized error.

### Focused Validation

- `python -m pytest tests/test_confidence_ranking.py tests/test_webapp_audit.py -q`

## Agent 4: Dynamic Relationship Graph Agent

### Write-Owned Files

- `auto_valuation/learning/feature_space.py`
- `auto_valuation/learning/cross_industry.py`
- `auto_valuation/learning/relationship_graph.py` (new)
- `tests/test_dynamic_relationship_graph.py` (new)
- `auto_valuation/learning/SYMBOL_BRAIN_HANDOFF.md`

### Read-Only Context

- `webapp/data/knowledge_model.py`
- `auto_valuation/validation/shared_brain.py`
- `tests/test_learning_system.py`
- `SHARED_BRAIN_VALIDATION.md`

### Must Not Edit

- Any reserved cross-cutting file
- `auto_valuation/learning/calibrator.py`
- `auto_valuation/learning/confidence.py`
- `auto_valuation/learning/ledger.py`
- `webapp/data/eodhd_client.py`

### Prompt

> You are the Dynamic Relationship Graph Agent for this repository. Your job is to deepen the analog layer from static feature similarity into a time-varying cross-symbol relationship engine.
>
> Start from: `auto_valuation/learning/feature_space.py`, `auto_valuation/learning/cross_industry.py`, `webapp/data/knowledge_model.py`, `auto_valuation/validation/shared_brain.py`.
>
> Write only to: `auto_valuation/learning/feature_space.py`, `auto_valuation/learning/cross_industry.py`, `auto_valuation/learning/relationship_graph.py`, `tests/test_dynamic_relationship_graph.py`, and `auto_valuation/learning/SYMBOL_BRAIN_HANDOFF.md`. Do not edit `webapp/data/knowledge_model.py`; instead, publish a stable graph-facing API and document it.
>
> Current status: the symbol brain is real and explainable, but it is still centered on feature-space similarity, pattern matching, and weighted analog overlays. It does not yet act like a true evolving graph of cross-symbol relationships and regime transitions.
>
> Mission: build a dynamic relationship graph where company states become nodes and historically useful transfer links become edges. The graph should capture regime transitions, cross-sector analogs, maturity-stage changes, lead-lag effects, and cases where a company now resembles a different cohort more than its own history.
>
> Must do: create graph-ready state snapshots from the time-safe feature space; learn edge weights from realized predictive usefulness; allow different analog neighborhoods in different regimes; expose stable graph outputs that can feed analog ranking and global overlays when the evidence is robust; surface the top relationship paths and why they mattered.
>
> Constraints: no future leakage, no opaque graph scores without explanation, and no unstable relationship churn from tiny evidence changes.
>
> Deliverables: new graph or transition-learning module, focused stability/regime-switch tests, and an updated symbol-brain handoff doc with exact import and output contracts.
>
> Success condition: your focused tests pass, and the Revenue Growth Regime Agent and Acceptance and Operations Agent can consume your graph API without changing your owned files.

### Focused Validation

- `python -m pytest tests/test_dynamic_relationship_graph.py -q`

## Agent 5: Acceptance and Operations Agent

### Write-Owned Files

- `webapp/data/knowledge_model.py`
- `auto_valuation/validation/shared_brain.py`
- `auto_valuation/validation/__init__.py`
- `validate_shared_brain.py`
- `check.py`
- `auto_valuation/learning/__init__.py`
- `tests/test_learning_system.py`
- `tests/test_shared_brain_acceptance.py` (new)
- `README.md`
- `CHANGELOG.md`
- `SHARED_BRAIN_VALIDATION.md`

### Read-Only Inputs

- `auto_valuation/learning/revenue_regime.py`
- `auto_valuation/learning/bootstrap_history.py`
- `auto_valuation/learning/confidence_ranking.py`
- `auto_valuation/learning/relationship_graph.py`
- `auto_valuation/learning/GROWTH_REGIME_HANDOFF.md`
- `auto_valuation/learning/LEARNING_SPINE_CONTRACT.md`
- `auto_valuation/learning/CONFIDENCE_HANDOFF.md`
- `auto_valuation/learning/SYMBOL_BRAIN_HANDOFF.md`
- `tests/test_revenue_regime.py`
- `tests/test_learning_bootstrap.py`
- `tests/test_confidence_ranking.py`
- `tests/test_dynamic_relationship_graph.py`

### Must Not Edit

- `auto_valuation/learning/feature_space.py`
- `auto_valuation/learning/cross_industry.py`
- `auto_valuation/learning/calibrator.py`
- `auto_valuation/learning/_layered_calibrator.py`
- `auto_valuation/learning/confidence.py`
- `auto_valuation/learning/ledger.py`
- `auto_valuation/learning/maintenance.py`
- `auto_valuation/learning/postmortem.py`
- `webapp/data/eodhd_client.py`
- `webapp/templates/dashboard.html`

### Prompt

> You are the Acceptance and Operations Agent for this repository. Your job is to turn the shared-brain project from a provisional benchmark pass into a system with a hard acceptance gate, operational diagnostics, and a repeatable ready-or-not-ready answer.
>
> Start from: `auto_valuation/validation/shared_brain.py`, `validate_shared_brain.py`, `check.py`, `webapp/data/knowledge_model.py`, `tests/test_learning_system.py`, `README.md`, `CHANGELOG.md`, and the handoff docs produced by the other agents.
>
> Write only to the reserved cross-cutting files listed in this execution pack. Do not edit the feature, ledger, confidence, graph, or bootstrap internals owned by the other agents.
>
> Current status: the packaged validator now returns `provisional`, not `gap`. The remaining blocker for full acceptance is thin live evidence depth and any still-weak ranking or operational coverage that survives after the upstream agent work lands.
>
> Mission: wire the owned modules together, harden the acceptance gates, broaden the packaged benchmark where necessary, expose operational diagnostics for maintenance freshness and evidence coverage, and make the validator part of the normal repository quality bar.
>
> Must do: integrate the owned helper modules from the other agents into `webapp/data/knowledge_model.py` and the validator path; add explicit acceptance thresholds for revenue growth, margin, valuation error, confidence ranking, maintenance freshness, and postmortem coverage; expose diagnostics for stale maintenance, thin evidence, and missing postmortems; update repo-facing docs and summaries so the project status is honest and measurable.
>
> Constraints: do not move the goalposts to make the model look good, do not hide failing metrics, and do not weaken time-aware validation standards.
>
> Deliverables: stronger validator and health-check surfaces, final integration tests, documentation updates, and a final acceptance summary that says `accepted`, `provisional`, or `gap` with explicit reasons.
>
> Success condition: anyone can run `validate_shared_brain.py` and `check.py --strict-learning` and get a trustworthy, reproducible answer about whether the shared-brain system has reached full acceptance, provisional acceptance, or still has a concrete gap.

### Focused Validation

- `python -m pytest tests/test_learning_system.py -q -k SharedBrainValidationHarness`
- `python validate_shared_brain.py --no-diagnostics`
- `python validate_shared_brain.py`
- `python check.py --strict-learning`

## Ownership Summary

- Revenue Growth Regime Agent owns growth-learning internals and its focused tests.
- Live Evidence Bootstrap Agent owns ledger backfill, maintenance, postmortem generation, and evidence bootstrap.
- Confidence Ranking Agent owns confidence modeling and dashboard-facing confidence presentation.
- Dynamic Relationship Graph Agent owns feature-space and cross-symbol graph logic.
- Acceptance and Operations Agent owns the integration, validator, checks, repo-facing tests, and docs.

## Why This Split Avoids Collisions

- The hottest shared files are reserved to one agent.
- Each specialist agent works in an additive module plus dedicated test file.
- The Acceptance and Operations Agent is the only integrator, so there is no race over `knowledge_model.py`, validator logic, or the public repo status docs.
- The handoff docs make each additive module consumable without reopening ownership boundaries.