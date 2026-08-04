"""
================================================================================
RBC — Ghana NIC Risk-Based Capital / GIRBC solvency calculation
================================================================================
What this package does:
    Implements Ghana's National Insurance Commission GIRBC (General Insurance
    Risk-Based Capital) directive — the risk-module capital charges, the
    correlation-matrix aggregation into an overall Required Capital, and the
    resulting CAR (Capital Adequacy Ratio) — as an independent module
    alongside the older, simpler solvency-margin test (engine/rbc/legacy_solvency.py,
    added in a later phase) that Ghanaian non-life insurers still report as
    their headline figure during the transition period (GIRBC becomes
    mandatory 1 January 2027; see clients/qic/... FCR validation notes).

    Source of every formula/factor here: the real GIRBC directive and its
    official Excel SDR template, read in full and cross-checked against a
    real client's (QIC's) own actuary-prepared 2025 filing — see each
    module's docstring for exact provenance and any place this
    implementation deliberately simplifies or diverges from what the real
    Excel template does (disclosed, not silently guessed).

Modules:
    data_model.py       — QualifyingCapitalResources, InsuranceRiskExposures,
                           MarketRiskExposures, CreditRiskExposures,
                           OperationalRiskExposures (all pure data, no logic)
    correlation.py       — shared sqrt(quadratic-form) correlation-matrix
                           aggregation helper, used by every risk module
                           below AND by the top-level aggregator
    insurance_risk.py    — Non-Life Insurance risk (premium + claims reserve
                           by class) + Catastrophe risk
    market_risk.py       — Interest Rate, FX, Equity, Real Estate,
                           Right-of-Use Assets risk
    credit_risk.py       — Counterparty, Mortgage, and "Other" credit
                           exposure risk
    operational_risk.py  — Premium/Liability/Growth-based operational risk
    aggregation.py        — Combines the four module totals into Overall
                            Risk Charge, MCR, PCR, and CAR
================================================================================
"""
