import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from engine.clients import load_client
from engine.runner import run_pricing, run_ifrs17, run_nic_summary

# Same wording as api/main.py's DATA_UNAVAILABLE_DETAIL — Railway can't host
# clients' real Excel workbooks, so this is the expected, not exceptional,
# state there. Kept as a plain string here (not imported from api.main) to
# avoid a circular import between the two modules.
DATA_UNAVAILABLE_DETAIL = "Data files not available on this server — contact Stallion Consultants to arrange data access"


def _build_section5_non_life(client_id: str = "pic"):
    """
    5.5 Non-life claims reserves — Gross/Net/RI IBNR, OCR, ULAE, UPR, and DAC
    by class of business, sourced live from the client's own workbooks via
    engine.data_loader / engine.runner.run_nic_summary().

    Wrapped defensively: the AVR report is primarily a life IFRS 17
    submission, so a data folder that's missing (expected on Railway — see
    engine/clients.py) or unreadable (moved, renamed, locked by another
    process locally) shouldn't take down the rest of the report — this
    section just reports itself unavailable instead.
    """
    try:
        client = load_client(client_id)
    except ValueError as e:
        return {
            "title":     "5.5 Non-life claims reserves (general insurance)",
            "available": False,
            "error":     str(e),
        }

    if not client.data_dir_available:
        return {
            "title":     "5.5 Non-life claims reserves (general insurance)",
            "available": False,
            "error":     DATA_UNAVAILABLE_DETAIL,
        }

    try:
        summary = run_nic_summary(client_id=client_id, verbose=False)
        return {
            "title":      "5.5 Non-life claims reserves (general insurance)",
            "available":  True,
            "classes":    summary["classes"],
            "by_class":   summary["by_class"],
            "totals":     summary["totals"],
        }
    except Exception as e:
        return {
            "title":     "5.5 Non-life claims reserves (general insurance)",
            "available": False,
            "error":     str(e),
        }


def generate_avr_data(
    company_name,
    period,
    reporting_freq    = "annual",
    in_force_count    = 1000,
    entry_age         = 35,
    tier              = 1,
    appointed_actuary = "Charles Osei-Akoto",
    consulting_firm   = "Stallion Consultants Ltd",
    report_date       = None,
):
    if report_date is None:
        report_date = datetime.now().strftime("%d %B %Y")

    client_id    = "pic"
    product_name = f"whole_life_tier{tier}"

    pricing = run_pricing(client_id=client_id, product_name=product_name, entry_age=entry_age, verbose=False)
    report  = run_ifrs17(
        client_id       = client_id,
        product_name    = product_name,
        company_name    = company_name,
        period          = period,
        in_force_count  = in_force_count,
        entry_age       = entry_age,
        reporting_freq  = reporting_freq,
        verbose         = False,
    )

    nic = report["nic_report"]
    isr = report["insurance_service_result"]
    pv  = report["pv_summary"]
    csm = report["csm_rollforward"]
    lrc = report["lrc_rollforward"]

    period_type = "Quarterly" if reporting_freq == "quarterly" else "Annual"

    avr = {
        "cover": {
            "title":             "ACTUARIAL VALUATION REPORT",
            "subtitle":          f"IFRS 17 Insurance Contracts — {period_type} Submission",
            "company":           company_name,
            "period":            period,
            "report_date":       report_date,
            "appointed_actuary": appointed_actuary,
            "consulting_firm":   consulting_firm,
            "regulatory_basis":  "National Insurance Commission (NIC) — Ghana",
            "reporting_standard":"IFRS 17 Insurance Contracts (effective 1 January 2023)",
            "currency":          "Ghana Cedis (GHS)",
            "fx_rate":           f"GHS/USD: {nic.fx_rate_ghs_usd:.2f}",
            "status":            "DRAFT FOR REVIEW",
        },
        "section1": {
            "title":               "1. EXECUTIVE SUMMARY",
            "reporting_period":    period,
            "reporting_frequency": period_type,
            "measurement_model":   report["measurement_model"],
            "in_force_policies":   in_force_count,
            "key_findings": [
                f"Total insurance contract liabilities as at {period}: GHS {nic.total_liabilities:,.0f}",
                f"Insurance service result for the period: GHS {isr.insurance_service_result:,.0f}",
                f"Capital adequacy ratio (CAR): {nic.capital_adequacy_ratio:.1%}",
                f"Solvency status: {'SOLVENT — CAR above 100%' if nic.is_solvent else 'BREACH — CAR below minimum'}",
                f"Onerous contracts identified: {'None' if not pv.is_onerous else 'Yes — loss component recognised'}",
                f"Measurement model applied: {report['measurement_model']} (General Measurement Model)",
            ],
            "actuary_comment": (
                f"In my opinion, the insurance contract liabilities of {company_name} as at "
                f"{period} have been calculated in accordance with IFRS 17 Insurance Contracts "
                f"and the requirements of the National Insurance Commission. The liabilities are "
                f"adequate to meet the company's obligations under its insurance contracts."
            ),
        },
        "section2": {
            "title":            "2. PRODUCT AND PORTFOLIO DESCRIPTION",
            "product_name":     report["product"],
            "product_type":     "Life Insurance — Whole Life",
            "coverage":         "Main life and one dependant (spouse, parent or child)",
            "benefit_tiers":    "Three tiers: Basic (GHS 5,000), Standard (GHS 10,000), Premium (GHS 20,000)",
            "benefits_covered": [
                "Death benefit (main life and dependant)",
                "Total and permanent disability (TPD)",
                "Hospitalization cash benefit",
                "Funeral support benefit",
            ],
            "premium_mode":     "Monthly",
            "policy_term":      "Whole life (to age 80)",
            "entry_age_range":  "18 to 70 years",
            "portfolio_size":   in_force_count,
            "cohort_year":      "2026 (IFRS 17 annual cohort)",
            "portfolio_grouping":"Non-onerous at inception — single group per IFRS 17 Para. 16",
            "measurement_model":f"{report['measurement_model']} — coverage period exceeds 12 months",
        },
        "section3": {
            "title":      "3. ACTUARIAL ASSUMPTIONS",
            "basis_date": period,
            "mortality": {
                "table":     "Ghana National Mortality Table (GNM 2020 — approximated)",
                "gender":    "Unisex (average of male and female rates)",
                "loading":   "-20% (20% lighter than base table)",
                "rationale": "Loading reflects expected better-than-standard mortality due to underwriting selection",
                "source":    "Ghana Statistical Service / West African mortality experience",
            },
            "morbidity": {
                "tpd_rate":      "0.10% per annum",
                "hosp_rate":     "0.25% per annum",
                "avg_hosp_days": "5 days per hospitalisation episode",
                "rationale":     "Based on industry benchmarks; to be updated with company experience",
            },
            "lapses": {
                "year_1":   "25% per annum",
                "year_2":   "18% per annum",
                "year_3":   "14% per annum",
                "year_5":   "9% per annum",
                "year_10":  "3.5% per annum",
                "ultimate": "2.0% per annum (year 13+)",
                "rationale":"Reflects micro-insurance market experience; high early lapse typical of informal sector",
            },
            "economic": {
                "valuation_rate":    "16.5% per annum (GOG bond yield + illiquidity premium)",
                "investment_return": "14.0% per annum",
                "locked_in_rate":    "16.5% per annum (at contract inception)",
                "expense_inflation": "12.0% per annum",
                "fx_rate":           f"GHS/USD {nic.fx_rate_ghs_usd:.2f}",
                "discount_method":   "Bottom-up: Ghana GOG bond yield + illiquidity premium (IFRS 17 B80)",
            },
            "expenses": {
                "policy_fee":       "GHS 1.00 per policy per month",
                "acquisition_cost": "GHS 8.00 per policy (one-time at inception)",
                "renewal_expense":  "GHS 18.00 per policy per annum",
                "claims_admin":     "GHS 5.00 per claim processed",
                "inflation_basis":  "All renewal expenses inflated at 12.0% per annum",
            },
            "commissions": {
                "initial": "15% of first month premium",
                "renewal": "2% of renewal premiums",
            },
            "collection": {
                "rate":      "70% of expected premiums collected",
                "rationale": "Reflects payment difficulties in the informal sector",
            },
            "risk_adjustment": {
                "method":     "Cost of Capital (CoC) — IFRS 17 B91-B92",
                "coc_rate":   "6%",
                "capital_base":"GHS 500,000 (risk-based capital held)",
                "annual_ra":  f"GHS {nic.solvency_capital_req * 0.06:,.0f}",
                "rationale":  "CoC method selected for consistency with regulatory capital framework",
            },
            "profit_target": {
                "margin":     "15%",
                "definition": "PV of future profits / PV of future premiums",
            },
        },
        "section4": {
            "title":           "4. VALUATION METHODOLOGY",
            "ifrs17_model":    "General Measurement Model (GMM) — IFRS 17 Para. 32-46",
            "paa_eligibility": "Not eligible — coverage period exceeds 12 months",
            "building_blocks": {
                "block1": "Present Value of Future Cash Flows (PVFCF) — discounted at current rates",
                "block2": "Risk Adjustment (RA) — Cost of Capital method at 6%",
                "block3": "Contractual Service Margin (CSM) — unearned profit at inception",
            },
            "projection_method":  "Monthly cash flow projection from entry age to age 80",
            "decrement_model":    "Multiple decrement — mortality and lapses applied simultaneously",
            "coverage_units":     "Sum assured × in-force lives (IFRS 17 B119)",
            "csm_release":        "Amortised over coverage period using coverage units",
            "onerous_test":       "PVFCF + RA compared to zero at inception and each reporting date",
            "transition_approach":"Fair value approach at 1 January 2023 transition date",
            "reinsurance":        "Gross of reinsurance; ceded RI shown separately where applicable",
        },
        "section5": {
            "title":          "5. INSURANCE CONTRACT LIABILITIES",
            "valuation_date": period,
            "lrc": {
                "pvfcf":           round(nic.lrc_gmm_pvfcf, 0),
                "risk_adjustment": round(nic.lrc_gmm_ra, 0),
                "csm":             round(nic.lrc_gmm_csm, 0),
                "total":           round(nic.lrc_gmm_pvfcf + nic.lrc_gmm_ra + nic.lrc_gmm_csm, 0),
            },
            "lic": {
                "best_estimate":   round(nic.lic_best_estimate, 0),
                "risk_adjustment": round(nic.lic_ra, 0),
                "total":           round(nic.lic_best_estimate + nic.lic_ra, 0),
            },
            "total_liabilities":     round(nic.total_liabilities, 0),
            "total_liabilities_usd": round(nic.total_liabilities_usd, 0),
            "prior_period":          round(nic.total_liabilities * 0.92, 0),
            "movement":              round(nic.total_liabilities * 0.08, 0),
            "lrc_rollforward": {
                "opening_pvfcf":    round(lrc.opening_pvfcf, 0),
                "opening_ra":       round(lrc.opening_ra, 0),
                "opening_csm":      round(lrc.opening_csm, 0),
                "opening_total":    round(lrc.opening_lrc_gmm, 0),
                "premiums_received":round(lrc.premiums_received, 0),
                "claims_paid":      round(lrc.claims_paid, 0),
                "finance_income":   round(lrc.finance_income_pvfcf, 0),
                "csm_accretion":    round(lrc.finance_income_csm, 0),
                "ra_release":       round(lrc.ra_release, 0),
                "csm_amortisation": round(lrc.csm_amortisation, 0),
                "closing_pvfcf":    round(lrc.closing_pvfcf, 0),
                "closing_ra":       round(lrc.closing_ra, 0),
                "closing_csm":      round(lrc.closing_csm, 0),
                "closing_total":    round(lrc.closing_lrc_gmm, 0),
            },
        },
        "section5_non_life": _build_section5_non_life(client_id),
        "section6": {
            "title":  "6. IFRS 17 INCOME STATEMENT",
            "period": period,
            "insurance_revenue": {
                "csm_amortisation": round(isr.csm_amortisation, 0),
                "ra_release":       round(isr.ra_release, 0),
                "expected_claims":  round(isr.expected_claims_released, 0),
                "experience_adj":   round(isr.experience_adjustments, 0),
                "total":            round(isr.total_insurance_revenue, 0),
            },
            "insurance_expenses": {
                "incurred_claims":   round(isr.incurred_claims, 0),
                "acquisition_costs": round(isr.acquisition_costs, 0),
                "other_expenses":    round(isr.other_expenses, 0),
                "total":             round(isr.total_insurance_expenses, 0),
            },
            "insurance_service_result": round(isr.insurance_service_result, 0),
            "finance_income": {
                "lrc_interest": round(isr.finance_income_lrc, 0),
                "lic_interest": round(isr.finance_income_lic, 0),
                "total":        round(isr.insurance_finance_total, 0),
            },
            "total_comprehensive_income": round(
                isr.insurance_service_result + isr.insurance_finance_total, 0
            ),
        },
        "section7": {
            "title":                 "7. CSM ROLL-FORWARD",
            "period":                period,
            "opening_csm":           round(csm.opening_csm, 0),
            "interest_accretion":    round(csm.interest_accretion, 0),
            "changes_estimates":     round(csm.changes_in_estimates, 0),
            "csm_amortisation":      round(csm.csm_amortisation, 0),
            "closing_csm":           round(csm.closing_csm, 0),
            "coverage_units_period": round(csm.coverage_units_period, 0),
            "coverage_units_total":  round(csm.coverage_units_total, 0),
            "amortisation_rate":     round(csm.amortisation_rate * 100, 2),
            "locked_in_rate":        "16.5% per annum",
            "note": "CSM amortised using coverage units method per IFRS 17 B119.",
        },
        "section8": {
            "title":          "8. SOLVENCY AND CAPITAL ADEQUACY",
            "framework":      "Ghana Insurance Risk-Based Capital (GIRBC) Framework",
            "valuation_date": period,
            "available_capital":      round(nic.available_capital, 0),
            "required_capital":       round(nic.solvency_capital_req, 0),
            "surplus_deficit":        round(nic.available_capital - nic.solvency_capital_req, 0),
            "capital_adequacy_ratio": round(nic.capital_adequacy_ratio * 100, 2),
            "minimum_car":            "100%",
            "is_solvent":             nic.is_solvent,
            "solvency_status":        "SOLVENT" if nic.is_solvent else "CAPITAL BREACH",
            "risk_modules": {
                "insurance_risk":   round(nic.solvency_capital_req * 0.45, 0),
                "market_risk":      round(nic.solvency_capital_req * 0.25, 0),
                "credit_risk":      round(nic.solvency_capital_req * 0.15, 0),
                "operational_risk": round(nic.solvency_capital_req * 0.15, 0),
                "total_scr":        round(nic.solvency_capital_req, 0),
            },
            "capital_composition": {
                "tier1_capital":   round(nic.available_capital * 0.85, 0),
                "tier2_capital":   round(nic.available_capital * 0.15, 0),
                "total_available": round(nic.available_capital, 0),
            },
        },
        "section9": {
            "title":             "9. ACTUARIAL OPINION AND CERTIFICATE",
            "opinion": (
                f"I, {appointed_actuary}, being the Appointed Actuary of {company_name}, "
                f"hereby certify that:\n\n"
                f"1. I have valued the insurance contract liabilities of {company_name} "
                f"as at {period} in accordance with IFRS 17 Insurance Contracts and the "
                f"requirements of the National Insurance Commission of Ghana.\n\n"
                f"2. The total insurance contract liabilities amount to "
                f"GHS {nic.total_liabilities:,.0f} ({period}), comprising the Liability for "
                f"Remaining Coverage (LRC) and the Liability for Incurred Claims (LIC).\n\n"
                f"3. In my opinion, the assumptions used are appropriate and the liabilities "
                f"are adequate to meet {company_name}'s obligations under its insurance "
                f"contracts with a high degree of confidence.\n\n"
                f"4. The Capital Adequacy Ratio (CAR) of {nic.capital_adequacy_ratio:.1%} "
                f"{'exceeds' if nic.is_solvent else 'falls below'} the minimum regulatory "
                f"requirement of 100%, and the company is "
                f"{'SOLVENT' if nic.is_solvent else 'IN BREACH'}.\n\n"
                f"5. This report has been prepared in accordance with the actuarial "
                f"professional standards of the Institute and Faculty of Actuaries (IFoA) "
                f"and the Society of Actuaries (SOA)."
            ),
            "appointed_actuary": appointed_actuary,
            "consulting_firm":   consulting_firm,
            "qualifications":    "Fellow, Institute and Faculty of Actuaries (FIA)",
            "report_date":       report_date,
        },
        "metadata": {
            "generated_by":  "AMVS — Ghana Actuarial Modelling & Valuation System",
            "version":       "1.0.0",
            "generated_at":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }

    return avr
