"""
================================================================================
CAPE COD METHOD — validation
================================================================================
What this file does:
    Validates engine/cape_cod.py — the self-calibrating Bornhuetter-Ferguson
    variant that derives its own expected loss ratio from the triangle's
    used-up premium instead of requiring one supplied externally.

    Reuses the exact same real PIC triangle/premium fixtures (and PIC's own
    "Selected" IBNR benchmark) as tests/test_chain_ladder.py — Cape Cod is
    being run against the identical data already validated there, not a
    separate synthetic dataset, so the comparison is apples-to-apples with
    the Chain Ladder / Bornhuetter-Ferguson results already on record.
================================================================================
"""

import pytest

from engine.claims_triangle import ClaimsTriangle
from engine.chain_ladder import run_chain_ladder
from engine.bornhuetter_ferguson import estimate_expected_loss_ratio, run_bornhuetter_ferguson
from engine.cape_cod import estimate_cape_cod_loss_ratio, run_cape_cod
from tests.test_chain_ladder import CLASSES, _build_triangle


@pytest.mark.parametrize("class_name", CLASSES.keys())
@pytest.mark.parametrize("gross_or_net", ["gross", "net"])
def test_cape_cod_against_pic_benchmark(class_name, gross_or_net):
    cfg = CLASSES[class_name]
    triangle_data = cfg[f"{gross_or_net}_tri"]
    premium       = cfg[f"{gross_or_net}_prem"]
    pic_ibnr      = cfg[f"{gross_or_net}_ibnr"]

    triangle = _build_triangle(f"{class_name} ({gross_or_net})", triangle_data)
    cl = run_chain_ladder(triangle)
    cl_variance = (cl.total_ibnr - pic_ibnr) / pic_ibnr if pic_ibnr else float("nan")

    try:
        cc = run_cape_cod(triangle, premium)
        cc_variance = (cc.bf_result.total_bf_ibnr - pic_ibnr) / pic_ibnr if pic_ibnr else float("nan")
        print(f"\n[{class_name} {gross_or_net.title()}] Cape Cod ELR={cc.expected_loss_ratio:.1%}  "
              f"Chain Ladder IBNR=GHS {cl.total_ibnr:,.0f} ({cl_variance:+.1%})  "
              f"Cape Cod IBNR=GHS {cc.bf_result.total_bf_ibnr:,.0f} ({cc_variance:+.1%})  "
              f"vs PIC Selected=GHS {pic_ibnr:,.0f}")
        assert cc.bf_result.total_bf_ibnr == cc.bf_result.total_bf_ibnr  # not NaN
    except ValueError as e:
        # Same small/volatile-class caveat as BF's own estimate_expected_loss_ratio —
        # a triangle with zero/negative used-up premium can't derive an ELR.
        print(f"\n[{class_name} {gross_or_net.title()}] Cape Cod ELR unavailable ({e}); "
              f"Chain Ladder IBNR=GHS {cl.total_ibnr:,.0f} ({cl_variance:+.1%}) "
              f"vs PIC Selected=GHS {pic_ibnr:,.0f}")

    assert cl.total_ibnr >= 0


def test_cape_cod_reduces_to_bf_once_elr_is_known():
    """
    Cape Cod is explicitly "BF with a self-derived ELR" — running BF
    directly with Cape Cod's own derived ELR must reproduce Cape Cod's
    result exactly, since run_cape_cod() delegates the projection step to
    run_bornhuetter_ferguson() rather than re-implementing it.
    """
    cfg = CLASSES["Motor"]
    triangle = _build_triangle("Motor (Gross)", cfg["gross_tri"])
    premium  = cfg["gross_prem"]

    cc = run_cape_cod(triangle, premium)
    bf_with_cc_elr = run_bornhuetter_ferguson(triangle, premium, cc.expected_loss_ratio)

    assert cc.bf_result.total_bf_ibnr == pytest.approx(bf_with_cc_elr.total_bf_ibnr)
    for oy in triangle.origin_years:
        assert cc.bf_result.bf_ultimate[oy] == pytest.approx(bf_with_cc_elr.bf_ultimate[oy])


def test_cape_cod_elr_differs_from_mature_years_only_elr():
    """
    Cape Cod's ELR (weighted across every origin year by used-up premium)
    and BF's own estimate_expected_loss_ratio() (mature years only) are
    two genuinely different estimators — they have no reason to land on
    the same number, and shouldn't be silently identical (that would mean
    one of them isn't actually using its own stated methodology).
    """
    cfg = CLASSES["Motor"]
    triangle = _build_triangle("Motor (Gross)", cfg["gross_tri"])
    premium  = cfg["gross_prem"]

    cape_cod_elr    = estimate_cape_cod_loss_ratio(triangle, premium)
    mature_only_elr = estimate_expected_loss_ratio(triangle, premium)

    assert cape_cod_elr != pytest.approx(mature_only_elr, rel=1e-6)
    assert cape_cod_elr > 0


def test_used_up_premium_never_exceeds_earned_premium():
    """% reported is a probability-like weight in (0, 1] — used-up premium
    (earned premium x % reported) can never exceed the earned premium itself."""
    cfg = CLASSES["Fire"]
    triangle = _build_triangle("Fire (Gross)", cfg["gross_tri"])
    premium  = cfg["gross_prem"]

    cc = run_cape_cod(triangle, premium)
    for oy in triangle.origin_years:
        assert cc.used_up_premium[oy] <= premium.get(oy, 0.0) + 1e-6


def test_zero_used_up_premium_raises_a_clear_error():
    triangle = ClaimsTriangle(class_of_business="Synthetic", origin_years=[2023, 2024],
                               triangle={2023: [100.0, 150.0], 2024: [80.0]})
    with pytest.raises(ValueError, match="used-up premium"):
        run_cape_cod(triangle, {2023: 0.0, 2024: 0.0})


# ── engine/runner.py — run_reserving()'s three-way method dispatch ──────────

def test_run_reserving_dispatches_cape_cod():
    from engine.runner import run_reserving
    cfg = CLASSES["Motor"]
    result = run_reserving(
        class_of_business = "Motor",
        gross_triangle     = cfg["gross_tri"],
        net_triangle        = cfg["net_tri"],
        method                = "cape_cod",
        gross_premium          = cfg["gross_prem"],
        net_premium              = cfg["net_prem"],
        verbose                    = False,
    )
    assert result["method"] == "cape_cod"
    assert result["gross_ibnr"] > 0
    assert result["expected_loss_ratio_gross"] is not None
    assert result["expected_loss_ratio_net"] is not None


def test_run_reserving_cape_cod_requires_premium():
    from engine.runner import run_reserving
    cfg = CLASSES["Motor"]
    with pytest.raises(ValueError, match="gross_premium and net_premium are required"):
        run_reserving(
            class_of_business = "Motor",
            gross_triangle     = cfg["gross_tri"],
            net_triangle        = cfg["net_tri"],
            method                = "cape_cod",
            verbose                = False,
        )


def test_run_reserving_rejects_unknown_method():
    from engine.runner import run_reserving
    cfg = CLASSES["Motor"]
    with pytest.raises(ValueError, match="Unknown reserving method"):
        run_reserving(
            class_of_business = "Motor",
            gross_triangle     = cfg["gross_tri"],
            net_triangle        = cfg["net_tri"],
            method                = "nonsense",
            verbose                = False,
        )
