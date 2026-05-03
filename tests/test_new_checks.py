"""Tests for validation/checks.py — new functions added this session."""
import pytest
from auto_valuation.validation.checks import (
    check_nci_materiality,
    check_pension_materiality,
    check_lease_wacc_materiality,
    validate_reinvestment_consistency,
    check_restatement_detection,
    check_price_freshness,
)


class TestCheckNciMateriality:
    def test_material_nci_warns(self):
        result = check_nci_materiality(
            minority_interest_mm=100,
            total_equity_mm=1_000,   # 10% → material
        )
        assert result.status == "WARN"
        assert result.value == pytest.approx(0.10)

    def test_immaterial_nci_passes(self):
        result = check_nci_materiality(
            minority_interest_mm=10,
            total_equity_mm=1_000,   # 1% → immaterial
        )
        assert result.status == "PASS"

    def test_zero_equity_passes(self):
        result = check_nci_materiality(minority_interest_mm=100, total_equity_mm=0)
        assert result.status == "PASS"

    def test_custom_threshold(self):
        result = check_nci_materiality(
            minority_interest_mm=30,
            total_equity_mm=1_000,   # 3% → below 5% default but above 2% custom
            warn_threshold=0.02,
        )
        assert result.status == "WARN"


class TestCheckPensionMateriality:
    def test_material_pension_warns(self):
        result = check_pension_materiality(
            pension_obligation_mm=200,
            total_assets_mm=2_000,   # 10% → material
        )
        assert result.status == "WARN"

    def test_immaterial_pension_passes(self):
        result = check_pension_materiality(
            pension_obligation_mm=50,
            total_assets_mm=5_000,   # 1% → passes
        )
        assert result.status == "PASS"

    def test_zero_assets_passes(self):
        result = check_pension_materiality(pension_obligation_mm=100, total_assets_mm=0)
        assert result.status == "PASS"

    def test_negative_pension_uses_abs(self):
        # Negative value should still use abs() for ratio calculation
        result = check_pension_materiality(
            pension_obligation_mm=-200,
            total_assets_mm=2_000,
        )
        assert result.status == "WARN"


class TestCheckLeaseWaccMateriality:
    def test_material_leases_warn(self):
        result = check_lease_wacc_materiality(
            finance_leases_mm=300,
            total_debt_mm=1_000,   # 30% → material
        )
        assert result.status == "WARN"
        assert "lease" in result.message.lower() or "WACC" in result.message

    def test_immaterial_leases_pass(self):
        result = check_lease_wacc_materiality(
            finance_leases_mm=50,
            total_debt_mm=1_000,   # 5% < 10%
        )
        assert result.status == "PASS"

    def test_zero_debt_passes(self):
        result = check_lease_wacc_materiality(finance_leases_mm=100, total_debt_mm=0)
        assert result.status == "PASS"

    def test_zero_leases_passes(self):
        result = check_lease_wacc_materiality(finance_leases_mm=0, total_debt_mm=1_000)
        assert result.status == "PASS"


class TestValidateReinvestmentConsistency:
    def test_consistent_passes(self):
        # g = ROIC × RR → 0.15 × 0.333 ≈ 5% = growth_rate
        result = validate_reinvestment_consistency(
            reinvestment_rate=0.333,
            roic=0.15,
            growth_rate=0.05,
        )
        assert result.status == "PASS"

    def test_inconsistent_warns(self):
        # Implied g = 0.10 × 0.50 = 5%; modelled growth = 15% → big gap
        result = validate_reinvestment_consistency(
            reinvestment_rate=0.50,
            roic=0.10,
            growth_rate=0.15,
        )
        assert result.status == "WARN"

    def test_custom_tolerance(self):
        # Gap of 2% with tight tolerance of 1% → warn
        result = validate_reinvestment_consistency(
            reinvestment_rate=0.40,
            roic=0.10,   # implied = 4%
            growth_rate=0.06,   # modelled = 6% → gap = 2%
            tolerance=0.01,
        )
        assert result.status == "WARN"


class TestCheckRestatementDetection:
    def test_large_jump_flagged(self):
        stmts = [
            {"revenue": 5_000, "calendarYear": "2023"},
            {"revenue": 2_000, "calendarYear": "2022"},   # 150% jump
        ]
        results = check_restatement_detection(stmts, revenue_jump_threshold=0.30)
        assert len(results) >= 1
        assert any(r.status == "WARN" for r in results)

    def test_normal_growth_no_flag(self):
        stmts = [
            {"revenue": 5_000, "calendarYear": "2023"},
            {"revenue": 4_700, "calendarYear": "2022"},   # ~6% growth
        ]
        results = check_restatement_detection(stmts)
        assert len(results) == 0

    def test_empty_input(self):
        results = check_restatement_detection([])
        assert results == []


class TestCheckPriceFreshness:
    def test_fresh_price_passes(self):
        import datetime
        today = datetime.date.today().isoformat()
        result = check_price_freshness(today, stale_days=5)
        assert result.status == "PASS"

    def test_stale_price_warns(self):
        result = check_price_freshness("2000-01-01", stale_days=5)
        assert result.status == "WARN"
        assert result.value > 5

    def test_future_price_warns(self):
        result = check_price_freshness("2099-01-01")
        assert result.status == "WARN"

    def test_invalid_date_warns(self):
        result = check_price_freshness("not-a-date")
        assert result.status == "WARN"
