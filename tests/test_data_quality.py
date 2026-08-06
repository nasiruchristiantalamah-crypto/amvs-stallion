"""
================================================================================
DATA QUALITY — validation
================================================================================
What this file does:
    Validates engine/data_quality.py — the pre-flight checks run against
    uploaded/loaded claims triangle and premium data before it's fed into
    Chain Ladder / Bornhuetter-Ferguson / Cape Cod. Covers each check in
    isolation with small synthetic triangles (so the exact trigger
    condition is unambiguous), then confirms the real PIC fixtures already
    used throughout test_chain_ladder.py behave as expected — including
    the genuine (not a bug) Net > Gross case on PIC's own Fire class,
    which is what pushed net_exceeds_gross from "error" to "warning"
    severity in the first place.
================================================================================
"""

import pytest

from engine.data_quality import check_triangle_quality
from tests.test_chain_ladder import CLASSES


def _issues_of(issues, check):
    return [i for i in issues if i.check == check]


# ── Individual checks, isolated on small synthetic triangles ────────────────

def test_decreasing_cumulative_is_flagged():
    gross = {2023: [100.0, 90.0]}   # falls from age 0 to age 1
    net   = {2023: [80.0, 70.0]}
    issues = check_triangle_quality("Test", gross, net)
    hits = _issues_of(issues, "decreasing_cumulative")
    assert len(hits) == 2  # once for gross, once for net
    assert all(i.origin_year == 2023 for i in hits)


def test_monotonic_triangle_has_no_decreasing_flag():
    gross = {2023: [100.0, 150.0, 150.0]}
    net   = {2023: [80.0, 120.0, 120.0]}
    issues = check_triangle_quality("Test", gross, net)
    assert _issues_of(issues, "decreasing_cumulative") == []


def test_negative_claims_are_flagged():
    gross = {2023: [-50.0, 100.0]}
    net   = {2023: [10.0, 20.0]}
    issues = check_triangle_quality("Test", gross, net)
    hits = _issues_of(issues, "negative_claims")
    assert len(hits) == 1
    assert hits[0].basis == "gross"


def test_missing_premium_for_a_triangle_year_is_flagged():
    gross = {2023: [100.0], 2024: [50.0]}
    net   = {2023: [80.0], 2024: [40.0]}
    issues = check_triangle_quality("Test", gross, net, gross_premium={2023: 1000.0}, net_premium={2023: 800.0})
    hits = _issues_of(issues, "missing_premium")
    # 2024 is missing from both gross_premium and net_premium.
    assert {(i.origin_year, i.basis) for i in hits} == {(2024, "gross"), (2024, "net")}


def test_zero_premium_counts_as_missing():
    gross = {2023: [100.0]}
    net   = {2023: [80.0]}
    issues = check_triangle_quality("Test", gross, net, gross_premium={2023: 0.0}, net_premium={2023: 500.0})
    hits = _issues_of(issues, "missing_premium")
    assert any(i.origin_year == 2023 and i.basis == "gross" for i in hits)


def test_orphan_premium_is_flagged():
    gross = {2023: [100.0]}
    net   = {2023: [80.0]}
    issues = check_triangle_quality("Test", gross, net, gross_premium={2023: 1000.0, 2022: 500.0}, net_premium={2023: 800.0})
    hits = _issues_of(issues, "orphan_premium")
    assert len(hits) == 1
    assert hits[0].origin_year == 2022 and hits[0].basis == "gross"


def test_no_premium_supplied_means_no_premium_checks_at_all():
    gross = {2023: [100.0]}
    net   = {2023: [80.0]}
    issues = check_triangle_quality("Test", gross, net)  # premium omitted entirely
    assert _issues_of(issues, "missing_premium") == []
    assert _issues_of(issues, "orphan_premium") == []


def test_net_exceeds_gross_is_flagged_as_a_warning_not_an_error():
    gross = {2023: [100.0]}
    net   = {2023: [150.0]}   # net > gross
    issues = check_triangle_quality("Test", gross, net)
    hits = _issues_of(issues, "net_exceeds_gross")
    assert len(hits) == 1
    assert hits[0].severity == "warning"


def test_net_premium_exceeding_gross_premium_is_flagged():
    gross = {2023: [100.0]}
    net   = {2023: [80.0]}
    issues = check_triangle_quality("Test", gross, net, gross_premium={2023: 1000.0}, net_premium={2023: 1500.0})
    assert len(_issues_of(issues, "net_exceeds_gross")) == 1


def test_outlier_development_factor_is_flagged():
    # Three origin years develop ~1.2x at age 0->1; one spikes to 10x.
    gross = {
        2020: [100.0, 120.0, 120.0, 120.0],
        2021: [100.0, 118.0, 118.0],
        2022: [100.0, 1000.0],   # the outlier
        2023: [100.0],
    }
    net = {y: list(v) for y, v in gross.items()}
    issues = check_triangle_quality("Test", gross, net)
    hits = _issues_of(issues, "outlier_development_factor")
    assert any(i.origin_year == 2022 and i.basis == "gross" for i in hits)
    # The stable years (~1.2x) shouldn't themselves be flagged.
    assert not any(i.origin_year == 2020 for i in hits)


def test_uniform_development_has_no_outlier_flag():
    gross = {2020: [100.0, 150.0, 150.0], 2021: [100.0, 150.0], 2022: [100.0]}
    net   = {y: list(v) for y, v in gross.items()}
    issues = check_triangle_quality("Test", gross, net)
    assert _issues_of(issues, "outlier_development_factor") == []


def test_malformed_triangle_shape_is_skipped_not_double_flagged():
    """
    A triangle whose shape ClaimsTriangle.validate() would itself reject
    (a newer origin year with MORE development than an older one) should
    not crash the outlier check — that's ClaimsTriangle.validate()'s job,
    not this module's, so the outlier check just skips it quietly.
    """
    gross = {2020: [100.0], 2021: [100.0, 150.0, 200.0]}  # invalid shape
    net   = {2020: [80.0], 2021: [80.0, 120.0, 160.0]}
    issues = check_triangle_quality("Test", gross, net)  # must not raise
    assert _issues_of(issues, "outlier_development_factor") == []


# ── Real PIC fixtures — confirms this actually finds something meaningful ───

def test_accident_net_flags_the_real_missing_2023_2024_premium():
    """
    Accident's Net premium fixture (test_chain_ladder.py) genuinely has no
    positive premium for 2023/2024 — this is the concrete real-world case
    that motivated the missing_premium check in the first place.
    """
    cfg = CLASSES["Accident"]
    issues = check_triangle_quality("Accident", cfg["gross_tri"], cfg["net_tri"], cfg["gross_prem"], cfg["net_prem"])
    hits = _issues_of(issues, "missing_premium")
    flagged_years = {(i.origin_year, i.basis) for i in hits}
    assert (2023, "net") in flagged_years
    assert (2024, "net") in flagged_years


def test_fire_net_exceeds_gross_is_flagged_but_not_fatal():
    """
    PIC's real Fire triangle genuinely has Net > Gross at several origin
    years (a legitimate treaty-timing artifact, not a parsing bug — this
    triangle already reproduces PIC's own Selected Net IBNR almost exactly
    elsewhere in the test suite, so it's known-good data). The check must
    flag it as a warning, not treat it as blocking or unexpected.
    """
    cfg = CLASSES["Fire"]
    issues = check_triangle_quality("Fire", cfg["gross_tri"], cfg["net_tri"], cfg["gross_prem"], cfg["net_prem"])
    hits = _issues_of(issues, "net_exceeds_gross")
    assert len(hits) > 0
    assert all(i.severity == "warning" for i in hits)


def test_motor_is_clean():
    """Motor — the largest, most stable class — should have no flagged issues at all."""
    cfg = CLASSES["Motor"]
    issues = check_triangle_quality("Motor", cfg["gross_tri"], cfg["net_tri"], cfg["gross_prem"], cfg["net_prem"])
    assert issues == []


# ── engine/runner.py wiring ───────────────────────────────────────────────

def test_run_reserving_surfaces_data_quality_issues():
    from engine.runner import run_reserving
    cfg = CLASSES["Accident"]
    result = run_reserving(
        class_of_business="Accident", gross_triangle=cfg["gross_tri"], net_triangle=cfg["net_tri"],
        method="chain_ladder", gross_premium=cfg["gross_prem"], net_premium=cfg["net_prem"], verbose=False,
    )
    assert "data_quality_issues" in result
    assert any(i["check"] == "missing_premium" for i in result["data_quality_issues"])
    # Must be plain JSON-serializable dicts, not dataclass instances.
    assert all(isinstance(i, dict) for i in result["data_quality_issues"])
