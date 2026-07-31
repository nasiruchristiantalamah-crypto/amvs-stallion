"""
================================================================================
MAIN RUNNER — AMVS Entry Point
================================================================================
What this file does:
    Ties all engine modules together into one clean entry point.

    You call run_pricing() or run_ifrs17() and the system:
        1. Loads assumptions
        2. Runs decrement projection
        3. Calculates cash flows
        4. Discounts to present values
        5. Solves for premium (if pricing)
        6. Produces IFRS 17 numbers (if valuation)
        7. Returns everything in a clean structured result

    This is the interface between:
        - The web application (React + FastAPI) → calls these functions
        - The calculation engine (this file) → runs the maths

Usage examples:
    # Pricing run — find premium for age 35, Tier 1
    result = run_pricing(client_id="pic", product_name="whole_life_tier1", entry_age=35)
    print(f"Monthly premium: GHS {result['monthly_premium']:.2f}")

    # IFRS 17 run — produce year-end numbers for a company
    result = run_ifrs17(
        client_id       = "pic",
        product_name    = "whole_life_tier1",
        period          = "FY2025",
        in_force_count  = 500,
        entry_age       = 40,
    )

    # Rate table — price all ages
    table = run_rate_table(client_id="pic", product_name="whole_life_tier1")
================================================================================
"""

import time
from typing import Dict, List, Optional

from engine.assumptions import ReportingFrequency
from engine.assumptions_store import load_assumptions
from engine.clients import load_product
from engine.decrement import run_decrement_projection, summarise_decrement
from engine.cashflows import calculate_cash_flows
from engine.present_value import calculate_present_values
from engine.pricing import solve_premium, build_rate_table
from engine.ifrs17 import generate_ifrs17_report
from engine.rollforward_store import load_prior_closing, save_closing_snapshot
from engine.claims_triangle import ClaimsTriangle
from engine.chain_ladder import run_chain_ladder
from engine.bornhuetter_ferguson import estimate_expected_loss_ratio, run_blended_reserving


def _load_assumptions_and_product(client_id: str, product_name: str, assumptions_version: str = "current"):
    assumptions = load_assumptions(client_id, product_name, assumptions_version)
    product     = load_product(client_id, product_name)
    return assumptions, product


def run_pricing(
    client_id:            str,
    product_name:         str,
    assumptions_version:  str            = "current",
    entry_age:            Optional[int]  = None,
    mortality_loading:    Optional[float]= None,
    target_margin:        Optional[float]= None,
    verbose:              bool           = True,
) -> dict:
    """
    Run a complete pricing calculation for any client's product.

    Parameters:
        client_id            : Which insurer (clients/<client_id>/)
        product_name         : Which product (clients/<client_id>/products/<product_name>.yaml)
        assumptions_version  : Which saved assumption snapshot to use ("current" = active version)
        entry_age            : Override the entry age from the saved assumptions, if given
        mortality_loading    : Override the mortality loading, if given
        target_margin        : Override the target profit margin, if given
        verbose               : Print progress

    Returns:
        Dictionary with all pricing results
    """
    start = time.time()

    assumptions, product = _load_assumptions_and_product(client_id, product_name, assumptions_version)

    if entry_age is not None:
        assumptions.entry_age_main = entry_age
    if mortality_loading is not None:
        assumptions.mortality_loading = mortality_loading
    if target_margin is not None:
        assumptions.target_profit_margin = target_margin

    if verbose:
        print(f"\n{'='*60}")
        print(f"  AMVS PRICING RUN")
        print(f"  Client: {client_id}  |  Product: {product.name}")
        print(f"  Entry Age: {assumptions.entry_age_main}  |  Dependants: {[d.relationship for d in product.dependants]}")
        print(f"  Target Margin: {assumptions.target_profit_margin:.0%}")
        print(f"{'='*60}")

    # ── Run decrement projection ────────────────────────────────────────────
    if verbose:
        print(f"\n  Step 1: Running decrement projection...")
    dec_rows = run_decrement_projection(assumptions, product)
    dec_summary = summarise_decrement(dec_rows)
    if verbose:
        print(f"    {dec_summary['total_months']} months projected")
        print(f"    Expected deaths: {dec_summary['total_deaths_main']:.4f} per policy")
        print(f"    Year-1 lapse rate: {dec_summary['first_year_lapse']:.2%}")

    # ── Solve for premium ───────────────────────────────────────────────────
    if verbose:
        print(f"\n  Step 2: Solving for premium...")
    premium, pv = solve_premium(assumptions, product, assumptions.target_profit_margin)
    if verbose:
        print(f"    Monthly premium: GHS {premium:.2f}")
        print(f"    Annual premium:  GHS {premium * 12:.2f}")
        print(f"    Profit margin:   {pv.profit_margin:.2%}")

    # ── IFRS 17 numbers ─────────────────────────────────────────────────────
    if verbose:
        print(f"\n  Step 3: Calculating IFRS 17 building blocks...")
        print(f"    PVFCF at inception:   GHS {pv.pvfcf:,.2f}")
        print(f"    Risk Adjustment:      GHS {pv.risk_adjustment:,.2f}")
        print(f"    CSM at inception:     GHS {pv.csm_at_inception:,.2f}")
        print(f"    Onerous?              {'YES — LOSS' if pv.is_onerous else 'No'}")
        print(f"    LRC (total):          GHS {pv.lrc_total:,.2f}")

    elapsed = time.time() - start
    if verbose:
        print(f"\n  Completed in {elapsed:.2f} seconds")
        print(f"{'='*60}\n")

    return {
        "run_type":             "pricing",
        "client_id":            client_id,
        "product":              product.name,
        "assumptions_version":  assumptions_version,
        "entry_age":            assumptions.entry_age_main,
        "monthly_premium":      round(premium, 2),
        "annual_premium":       round(premium * 12, 2),
        "monthly_premium_usd":  round(premium / assumptions.fx_rate_ghs_usd, 4),
        "profit_margin":        round(pv.profit_margin, 4),
        "target_margin":        assumptions.target_profit_margin,
        "pv_premiums":          round(pv.pv_premiums, 2),
        "pv_benefits":          round(pv.pv_benefits, 2),
        "pv_expenses":          round(pv.pv_expenses, 2),
        "pv_profits":           round(pv.pv_profits, 2),
        "pvfcf":                round(pv.pvfcf, 2),
        "risk_adjustment":      round(pv.risk_adjustment, 2),
        "csm_at_inception":     round(pv.csm_at_inception, 2),
        "lrc_total":            round(pv.lrc_total, 2),
        "is_onerous":           pv.is_onerous,
        "loss_component":       round(pv.loss_component, 2),
        "decrement_summary":    dec_summary,
        "elapsed_seconds":      round(elapsed, 2),
        "currency":             assumptions.reporting_currency,
        "fx_rate_ghs_usd":      assumptions.fx_rate_ghs_usd,
    }


def run_ifrs17(
    client_id:            str,
    product_name:         str,
    period:               str            = "FY2025",
    period_index:         int            = 1,
    in_force_count:       int            = 1000,
    assumptions_version:  str            = "current",
    entry_age:            Optional[int]  = None,
    reporting_freq:       Optional[str]  = None,
    company_name:         Optional[str]  = None,
    use_prior_snapshot:   bool           = True,
    verbose:              bool           = True,
) -> dict:
    """
    Run a complete IFRS 17 valuation for any client's product, for a given
    reporting period, chaining to the prior period's real closing balance
    when one exists.

    Parameters:
        client_id            : Which insurer (clients/<client_id>/)
        product_name         : Which product
        period               : Reporting period label, e.g. "FY2025" or "Q1 2026"
        period_index         : 1 = first reporting period from inception, 2 =
                                the next one, etc. — see engine/ifrs17.py
        in_force_count       : Number of in-force policies
        assumptions_version  : Which saved assumption snapshot to use
        entry_age            : Override the entry age, if given
        reporting_freq       : Override "annual"/"quarterly"/"monthly", if given
        use_prior_snapshot   : Chain this period's opening balance to the
                                previous period's saved closing balance
                                (engine/rollforward_store.py). Set False to
                                re-measure from scratch.
        verbose               : Print progress

    Returns:
        Complete IFRS 17 report dictionary

    Usage:
        report = run_ifrs17(client_id="pic", product_name="whole_life_tier1",
                             period="FY2025", in_force_count=500)
        nic = report["nic_report"]
        print(f"Total liabilities: GHS {nic.total_liabilities:,.2f}")
        print(f"CAR: {nic.capital_adequacy_ratio:.1%}")
    """
    start = time.time()

    assumptions, product = _load_assumptions_and_product(client_id, product_name, assumptions_version)

    if entry_age is not None:
        assumptions.entry_age_main = entry_age
    if reporting_freq is not None:
        assumptions.reporting_frequency = ReportingFrequency(reporting_freq)
    if company_name is not None:
        assumptions.company_name = company_name

    if verbose:
        print(f"\n{'='*60}")
        print(f"  AMVS IFRS 17 VALUATION")
        print(f"  Client: {client_id}  |  Product: {product.name}")
        print(f"  Period:  {period} (index {period_index})  |  Frequency: {assumptions.reporting_frequency.value.title()}")
        print(f"  In-force policies: {in_force_count:,}")
        print(f"{'='*60}")

    # ── Solve for current premium, run full-lifetime projection once ────────
    if verbose:
        print(f"\n  Step 1: Pricing run...")
    dec_rows = run_decrement_projection(assumptions, product)
    premium, pv = solve_premium(assumptions, product)
    cf_rows = calculate_cash_flows(dec_rows, assumptions, product, premium)
    if verbose:
        print(f"    Premium: GHS {premium:.2f}/month")

    # ── Chain to the prior period's real closing balance ─────────────────────
    prior_closing = None
    if use_prior_snapshot and period_index > 1:
        prior_closing = load_prior_closing(client_id, product_name, period_index)
        if verbose and prior_closing is None:
            print(f"    No saved snapshot for period {period_index - 1} — re-measuring from scratch")

    # ── Generate IFRS 17 report ─────────────────────────────────────────────
    if verbose:
        print(f"\n  Step 2: Generating IFRS 17 report...")
    report = generate_ifrs17_report(
        assumptions, product, dec_rows, cf_rows, pv,
        period=period, period_index=period_index,
        prior_closing=prior_closing, in_force_count=in_force_count,
    )

    if use_prior_snapshot:
        save_closing_snapshot(client_id, product_name, period_index, report["closing_snapshot"])

    nic = report["nic_report"]
    isr = report["insurance_service_result"]

    if verbose:
        print(f"\n  IFRS 17 RESULTS ({period}):")
        print(f"    Total Liabilities:        GHS {nic.total_liabilities:>12,.0f}")
        print(f"      LRC (PVFCF):            GHS {nic.lrc_gmm_pvfcf:>12,.0f}")
        print(f"      LRC (Risk Adjustment):  GHS {nic.lrc_gmm_ra:>12,.0f}")
        print(f"      LRC (CSM):              GHS {nic.lrc_gmm_csm:>12,.0f}")
        print(f"      LIC (Best Estimate):    GHS {nic.lic_best_estimate:>12,.0f}")
        print(f"    Insurance Revenue:        GHS {isr.total_insurance_revenue:>12,.0f}")
        print(f"    Insurance Service Result: GHS {isr.insurance_service_result:>12,.0f}")
        print(f"    Capital Adequacy Ratio:   {nic.capital_adequacy_ratio:.1%}")
        print(f"    Solvent?                  {'YES' if nic.is_solvent else 'NO — BREACH!'}")
        print(f"    USD Liabilities:          USD {nic.total_liabilities_usd:>12,.0f}")

    elapsed = time.time() - start
    if verbose:
        print(f"\n  Completed in {elapsed:.2f} seconds")
        print(f"{'='*60}\n")

    return {**report, "client_id": client_id, "elapsed_seconds": round(elapsed, 2)}


def run_rate_table(
    client_id:            str,
    product_name:         str,
    assumptions_version:  str  = "current",
    age_start:            int  = 18,
    age_end:              int  = 70,
    verbose:              bool = True,
) -> dict:
    """
    Generate a complete premium rate table across a range of entry ages for
    any client's product. Python equivalent of your VBA
    GeneratePremiumRates() macro.

    Parameters:
        client_id    : Which insurer
        product_name : Which product
        age_start    : Starting age for table
        age_end      : Ending age for table

    Returns:
        Dict with age -> premium mapping
    """
    start = time.time()
    assumptions, product = _load_assumptions_and_product(client_id, product_name, assumptions_version)

    if verbose:
        print(f"\n{'='*60}")
        print(f"  AMVS RATE TABLE GENERATION")
        print(f"  Client: {client_id}  |  Product: {product.name}")
        print(f"  Ages: {age_start} to {age_end}")
        print(f"{'='*60}\n")

    table = build_rate_table(
        assumptions_template = assumptions,
        product               = product,
        age_range             = range(age_start, age_end + 1),
        verbose               = verbose,
    )

    elapsed = time.time() - start
    if verbose:
        print(f"\n  {'─'*50}")
        print(f"  RATE TABLE SUMMARY — {product.name}")
        print(f"  {'─'*50}")
        for age in range(age_start, age_end + 1, 5):
            if age in table and "error" not in table[age]:
                print(f"  Age {age:3d}:  GHS {table[age]['monthly_premium']:>8.2f}/month  |  "
                      f"GHS {table[age]['annual_premium']:>9.2f}/year")
        print(f"  {'─'*50}")
        print(f"  Completed in {elapsed:.1f} seconds")
        print(f"{'='*60}\n")

    return {
        "client_id":    client_id,
        "product":      product.name,
        "rate_table":   table,
        "elapsed":      round(elapsed, 2),
    }


def run_reserving(
    class_of_business:         str,
    gross_triangle:            Dict[int, List[float]],
    net_triangle:              Dict[int, List[float]],
    method:                    str             = "chain_ladder",
    gross_premium:             Optional[Dict[int, float]] = None,
    net_premium:               Optional[Dict[int, float]] = None,
    expected_loss_ratio_gross: Optional[float] = None,
    expected_loss_ratio_net:   Optional[float] = None,
    verbose:                   bool            = True,
) -> dict:
    """
    Run a general insurance (non-life) claims reserving projection for one
    class of business — the non-life equivalent of run_ifrs17() on the life
    side.

    Parameters:
        class_of_business  : Label for reporting, e.g. "Motor"
        gross_triangle      : origin_year -> cumulative incurred values by
                                development age (see engine/claims_triangle.py)
        net_triangle         : same shape, net of reinsurance
        method                : "chain_ladder" (default) or "bornhuetter_ferguson"
                                 Chain Ladder is the validated default — for 3
                                 of 4 classes tested against Provident
                                 Insurance's own workpapers it matched their
                                 selected IBNR closely or exactly. Only switch
                                 to Bornhuetter-Ferguson for a class with both
                                 a materially immature latest origin year and
                                 a large, stable premium history (see
                                 engine/bornhuetter_ferguson.py for why).
        gross_premium         : origin_year -> earned/written premium
                                 (required if method="bornhuetter_ferguson")
        net_premium            : same, net of reinsurance (required for BF)
        expected_loss_ratio_gross : a priori ELR for BF; estimated from the
                                     triangle's own mature years if omitted
        expected_loss_ratio_net    : same, net side
        verbose                     : Print progress

    Returns:
        Dictionary with Gross, Net, and RI (ceded) IBNR, plus the
        by-origin-year breakdown and the development factors used.
    """
    start = time.time()

    if method not in ("chain_ladder", "bornhuetter_ferguson"):
        raise ValueError(f"Unknown reserving method '{method}' — use 'chain_ladder' or 'bornhuetter_ferguson'")

    if verbose:
        print(f"\n{'='*60}")
        print(f"  AMVS RESERVING RUN — {class_of_business}")
        print(f"  Method: {method}")
        print(f"{'='*60}")

    gross_tri = ClaimsTriangle(class_of_business=class_of_business,
                                origin_years=sorted(gross_triangle.keys()), triangle=gross_triangle)
    net_tri   = ClaimsTriangle(class_of_business=class_of_business,
                                origin_years=sorted(net_triangle.keys()),   triangle=net_triangle)

    # ── Chain Ladder (always run — used directly, or as the BF baseline) ───
    cl_gross = run_chain_ladder(gross_tri)
    cl_net   = run_chain_ladder(net_tri)

    if method == "chain_ladder":
        gross_ibnr     = cl_gross.total_ibnr
        net_ibnr       = cl_net.total_ibnr
        gross_ultimate = {oy: round(v, 2) for oy, v in cl_gross.ultimate_losses.items()}
        net_ultimate   = {oy: round(v, 2) for oy, v in cl_net.ultimate_losses.items()}
        elr_gross_used = None
        elr_net_used   = None

    else:  # bornhuetter_ferguson
        if not gross_premium or not net_premium:
            raise ValueError("gross_premium and net_premium are required when method='bornhuetter_ferguson'")

        elr_gross_used = expected_loss_ratio_gross if expected_loss_ratio_gross is not None \
            else estimate_expected_loss_ratio(gross_tri, gross_premium)
        elr_net_used = expected_loss_ratio_net if expected_loss_ratio_net is not None \
            else estimate_expected_loss_ratio(net_tri, net_premium)

        blended_gross = run_blended_reserving(gross_tri, gross_premium, elr_gross_used)
        blended_net   = run_blended_reserving(net_tri,   net_premium,   elr_net_used)

        gross_ibnr     = blended_gross.total_blended_ibnr
        net_ibnr       = blended_net.total_blended_ibnr
        gross_ultimate = {oy: round(v, 2) for oy, v in blended_gross.blended_ultimate.items()}
        net_ultimate   = {oy: round(v, 2) for oy, v in blended_net.blended_ultimate.items()}

    # ── Gross / Net / RI split ──────────────────────────────────────────────
    # RI (ceded) IBNR is what reinsurers carry — the gap between gross and net
    ri_ibnr = gross_ibnr - net_ibnr

    if verbose:
        print(f"\n  Gross IBNR: GHS {gross_ibnr:,.2f}")
        print(f"  Net IBNR:   GHS {net_ibnr:,.2f}")
        print(f"  RI IBNR:    GHS {ri_ibnr:,.2f}")

    elapsed = time.time() - start
    if verbose:
        print(f"\n  Completed in {elapsed:.2f} seconds")
        print(f"{'='*60}\n")

    return {
        "run_type":                  "reserving",
        "class_of_business":         class_of_business,
        "method":                    method,
        "gross_ibnr":                round(gross_ibnr, 2),
        "net_ibnr":                  round(net_ibnr, 2),
        "ri_ibnr":                   round(ri_ibnr, 2),
        "gross_latest_cumulative":   round(cl_gross.total_latest_cumulative, 2),
        "net_latest_cumulative":     round(cl_net.total_latest_cumulative, 2),
        "gross_ultimate_by_year":    gross_ultimate,
        "net_ultimate_by_year":      net_ultimate,
        "development_factors_gross": [round(f, 4) for f in cl_gross.development_factors.age_to_age],
        "development_factors_net":   [round(f, 4) for f in cl_net.development_factors.age_to_age],
        "expected_loss_ratio_gross": round(elr_gross_used, 4) if elr_gross_used is not None else None,
        "expected_loss_ratio_net":   round(elr_net_used, 4)   if elr_net_used   is not None else None,
        "elapsed_seconds":           round(elapsed, 2),
    }


def run_nic_summary(client_id: str = "pic", verbose: bool = True) -> dict:
    """
    Build the full non-life NIC summary table — Gross, Net, and RI (ceded)
    IBNR, OCR, ULAE, UPR, and DAC by class of business — for all 4
    reserving classes (Motor, Fire, Accident, Others), for any configured
    client (see clients/<client_id>/client.yaml).

    Combines:
        - IBNR: run_reserving() (Chain Ladder) fed by data_loader.load_triangle()
        - OCR (case reserves): data_loader.load_ocr()
        - ULAE: data_loader.load_ulae() — not reinsured, so Net = Gross, RI = 0
                (PIC's own reporting convention — claims handling expense
                isn't ceded to reinsurers)
        - UPR / DAC: data_loader.load_upr_dac() (the client's own computed figures)

    RI (ceded to reinsurers) for IBNR/OCR/UPR/DAC = Gross - Net in each case.

    Returns:
        {
            "by_class": {"Motor": {"gross": {...}, "net": {...}, "ri": {...}}, ...},
            "totals":   {"gross": {...}, "net": {...}, "ri": {...}},
            "elapsed_seconds": float,
        }
    """
    from engine.data_loader import RESERVING_CLASSES, load_triangle, load_ocr, load_upr_dac, load_ulae

    start = time.time()
    if verbose:
        print(f"\n{'='*60}")
        print(f"  AMVS NIC NON-LIFE SUMMARY — all classes — client: {client_id}")
        print(f"{'='*60}")

    ocr_data     = load_ocr(client_id=client_id)
    upr_dac_data = load_upr_dac(client_id=client_id)
    ulae_data    = load_ulae(client_id=client_id)

    by_class: dict = {}
    for cls in RESERVING_CLASSES:
        tri = load_triangle(cls, client_id=client_id)
        reserving = run_reserving(
            class_of_business = cls,
            gross_triangle    = tri["gross_triangle"],
            net_triangle      = tri["net_triangle"],
            verbose           = False,
        )

        gross_ibnr = reserving["gross_ibnr"]
        net_ibnr   = reserving["net_ibnr"]
        gross_ocr  = ocr_data[cls]["gross"]
        net_ocr    = ocr_data[cls]["net"]
        gross_ulae = ulae_data[cls]
        net_ulae   = ulae_data[cls]  # not reinsured — Net = Gross, RI = 0
        gross_upr  = upr_dac_data[cls]["gross_upr"]
        net_upr    = upr_dac_data[cls]["net_upr"]
        gross_dac  = upr_dac_data[cls]["gross_dac"]
        net_dac    = upr_dac_data[cls]["net_dac"]

        by_class[cls] = {
            "gross": {
                "ibnr": round(gross_ibnr, 2), "ocr": round(gross_ocr, 2),
                "ulae": round(gross_ulae, 2), "upr":  round(gross_upr, 2),
                "dac":  round(gross_dac, 2),
            },
            "net": {
                "ibnr": round(net_ibnr, 2), "ocr": round(net_ocr, 2),
                "ulae": round(net_ulae, 2), "upr":  round(net_upr, 2),
                "dac":  round(net_dac, 2),
            },
            "ri": {
                "ibnr": round(gross_ibnr - net_ibnr, 2), "ocr": round(gross_ocr - net_ocr, 2),
                "ulae": 0.0,                             "upr": round(gross_upr - net_upr, 2),
                "dac":  round(gross_dac - net_dac, 2),
            },
        }

        if verbose:
            print(f"  {cls:10s}  Gross IBNR={gross_ibnr:>14,.0f}  Net IBNR={net_ibnr:>14,.0f}")

    totals = {"gross": {}, "net": {}, "ri": {}}
    for basis in ("gross", "net", "ri"):
        for metric in ("ibnr", "ocr", "ulae", "upr", "dac"):
            totals[basis][metric] = round(sum(by_class[cls][basis][metric] for cls in RESERVING_CLASSES), 2)

    elapsed = time.time() - start
    if verbose:
        print(f"\n  Total Gross IBNR: GHS {totals['gross']['ibnr']:,.2f}")
        print(f"  Total Net IBNR:   GHS {totals['net']['ibnr']:,.2f}")
        print(f"  Completed in {elapsed:.2f} seconds")
        print(f"{'='*60}\n")

    return {
        "run_type":        "nic_summary",
        "classes":         RESERVING_CLASSES,
        "by_class":        by_class,
        "totals":          totals,
        "elapsed_seconds": round(elapsed, 2),
    }


# ── Quick test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    """
    Run this file directly to test the engine:
        python -m engine.runner
    Or:
        cd /home/claude/amvs
        python engine/runner.py
    """
    print("\n" + "="*60)
    print("  AMVS — Ghana Actuarial Modelling & Valuation System")
    print("  Quick engine test")
    print("="*60)

    # Test 1: Price one policy
    result = run_pricing(client_id="pic", product_name="whole_life_tier1", entry_age=35, verbose=True)

    # Test 2: IFRS 17 run
    report = run_ifrs17(
        client_id       = "pic",
        product_name    = "whole_life_tier1",
        period          = "FY2025",
        in_force_count  = 500,
        entry_age       = 35,
        verbose         = True,
    )
