"""
================================================================================
NON-LIFE PAA STATEMENTS VALIDATION — Provident Insurance (PIC)
================================================================================
What this file does:
    Validates engine/ifrs17_nonlife.py's LRC calculation (UPR - DAC) against
    PIC's own real "Balance Sheet 2025" sheet in
    "Combined IFRS 17 Accounts_Dec 2025_v2.xlsb" — the same source used to
    validate the chain ladder engine (see test_chain_ladder.py).

    LRC = UPR - DAC doesn't depend on chain-ladder-derived IBNR at all (UPR
    and DAC come straight from data_loader.py's read of PIC's own UPR & DAC
    workbook), so this SHOULD match PIC's real figures almost exactly — a
    much tighter validation bar than the IBNR/LIC checks, which inherit the
    chain ladder's already-documented variance from PIC's judgmental
    "Selected" IBNR.

    PIC's real Balance Sheet 2025 only breaks out 6 classes (Accident,
    Bonds, Engineering, Fire Theft and Property, Marine, Motor) — this
    engine's reserving classes are Motor/Fire/Accident/Others, where
    "Others" = Bonds + Engineering + Marine (matching PIC's own IBNR
    Projection workbook's own "OTHERS" grouping — see engine/data_loader.py).

Run with:
    cd amvs
    pytest tests/test_ifrs17_nonlife.py -s
================================================================================
"""

from engine.ifrs17_nonlife import generate_nonlife_paa_statements

# PIC's real Balance Sheet 2025, "Liability for Remaining Coverage" row (GHS)
PIC_LRC_ACCIDENT    = 2_200_105.549
PIC_LRC_BONDS       = 6_151_924.027
PIC_LRC_ENGINEERING = 2_440_257.933
PIC_LRC_FIRE        = 3_269_928.517
PIC_LRC_MARINE      = 340_510.248
PIC_LRC_MOTOR       = 34_509_351.14
PIC_LRC_OTHERS      = PIC_LRC_BONDS + PIC_LRC_ENGINEERING + PIC_LRC_MARINE   # 8,932,692.21
PIC_LRC_TOTAL       = 48_912_077.41


def test_lrc_matches_pic_real_balance_sheet():
    """
    LRC = UPR - DAC should match PIC's real figures almost exactly (tight
    tolerance) since it's derived from PIC's own UPR/DAC data directly,
    not from the chain ladder.
    """
    result = generate_nonlife_paa_statements(verbose=False, use_discounting=False)
    by_class = result["by_class"]

    benchmarks = {
        "Motor":    PIC_LRC_MOTOR,
        "Fire":     PIC_LRC_FIRE,
        "Accident": PIC_LRC_ACCIDENT,
        "Others":   PIC_LRC_OTHERS,
    }

    print()
    total_computed = 0.0
    for cls, benchmark in benchmarks.items():
        computed = by_class[cls]["gross"].lrc
        variance = (computed - benchmark) / benchmark
        total_computed += computed
        print(f"[{cls}] Computed LRC = GHS {computed:,.2f}  vs PIC = GHS {benchmark:,.2f}  ({variance:+.4%})")
        assert abs(variance) < 0.001, f"{cls}: LRC variance {variance:.4%} exceeds 0.1% tolerance"

    total_variance = (total_computed - PIC_LRC_TOTAL) / PIC_LRC_TOTAL
    print(f"[TOTAL] Computed LRC = GHS {total_computed:,.2f}  vs PIC = GHS {PIC_LRC_TOTAL:,.2f}  ({total_variance:+.4%})")
    assert abs(total_variance) < 0.001


def test_gross_equals_net_plus_ri():
    """
    Internal consistency: RI (ceded) IBNR/OCR/UPR/DAC are already Gross-Net
    from run_nic_summary(), and RA/discounting are linear functions of
    (IBNR+OCR), so LIC and LRC should be exactly additive across Gross =
    Net + RI for every class.
    """
    result = generate_nonlife_paa_statements(verbose=False)
    for cls in result["classes"]:
        g = result["by_class"][cls]["gross"]
        n = result["by_class"][cls]["net"]
        r = result["by_class"][cls]["ri"]
        assert abs(g.lrc - (n.lrc + r.lrc)) < 0.01, f"{cls}: LRC gross != net+ri"
        assert abs(g.lic - (n.lic + r.lic)) < 0.01, f"{cls}: LIC gross != net+ri"


def test_total_liability_is_lrc_plus_lic():
    result = generate_nonlife_paa_statements(verbose=False)
    for cls in result["classes"]:
        for basis in ("gross", "net", "ri"):
            c = result["by_class"][cls][basis]
            assert abs(c.total_liability - (c.lrc + c.lic)) < 0.01


def test_onerous_flag_matches_negative_lrc():
    result = generate_nonlife_paa_statements(verbose=False)
    for cls in result["classes"]:
        for basis in ("gross", "net", "ri"):
            c = result["by_class"][cls][basis]
            assert c.is_onerous == (c.lrc < 0), f"{cls}/{basis}: onerous flag doesn't match LRC sign"
            if c.is_onerous:
                assert c.loss_component == round(-c.lrc, 2)
            else:
                assert c.loss_component == 0.0


def test_discounting_reduces_lic_when_enabled():
    """Discounting should always reduce (not increase) the best-estimate reserve."""
    with_discount    = generate_nonlife_paa_statements(verbose=False, use_discounting=True)
    without_discount = generate_nonlife_paa_statements(verbose=False, use_discounting=False)

    for cls in with_discount["classes"]:
        wd = with_discount["by_class"][cls]["gross"]
        nd = without_discount["by_class"][cls]["gross"]
        assert wd.effect_of_discounting <= 0
        if wd.ibnr + wd.ocr > 0:
            assert wd.lic < nd.lic
