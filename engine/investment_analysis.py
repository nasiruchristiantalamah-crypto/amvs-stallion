"""
================================================================================
INVESTMENT ANALYSIS — savings/investment fund projection
================================================================================
What this file does:
    Projects the SAVINGS portion of a policyholder's monthly contribution on
    an endowment-type product — the part left over after the risk premium
    that actually funds mortality/morbidity cover, i.e.
    investment_portion = monthly_contribution - risk_premium.

    This is a completely independent calculation from the risk-premium/
    reserve/CSM machinery elsewhere in engine/ — no mortality decrement is
    involved, matching how real micro-endowment products illustrate their
    own savings growth (a straight fund accumulation, not a probability-
    weighted cash flow).

    Methodology reverse-engineered from a real client workbook's own
    InvestAnalysis sheet (Impact Life / Phoenix Insurance, Afentoboa Plus):
    the fund accumulates monthly at a CREDITED rate distinct from both the
    valuation rate and the insurer's own investment return — the insurer
    typically earns more than it credits and keeps the spread. The
    recurrence below (opening x (1 + monthly credited rate) + contribution)
    is the standard future-value-of-an-ordinary-annuity mechanic and was
    confirmed to reproduce that workbook's own figures exactly (GHS 5/day
    contribution, GHS 120/month investment portion, 24 months at 5% p.a.
    credited -> GHS 3,022.31, matched to the cent).
================================================================================
"""

from typing import List


def project_investment_fund(
    monthly_contribution: float,
    risk_premium: float,
    credited_rate_pa: float,
    term_months: int,
) -> List[dict]:
    """
    Month-by-month projection of the savings fund for a single illustrative
    contribution level. The risk premium is deducted from the contribution
    first (it funds mortality/morbidity cover, not savings) — only the
    remainder ever earns interest.
    """
    investment_portion = max(0.0, monthly_contribution - risk_premium)
    monthly_rate = credited_rate_pa / 12
    rows = []
    balance = 0.0
    for month in range(1, term_months + 1):
        opening = balance
        interest = opening * monthly_rate
        closing = opening + investment_portion + interest
        rows.append({
            "month": month,
            "opening_balance": round(opening, 2),
            "contribution": round(monthly_contribution, 2),
            "investment_portion": round(investment_portion, 2),
            "interest_credited": round(interest, 2),
            "closing_balance": round(closing, 2),
        })
        balance = closing
    return rows


def summarise_investment_fund(rows: List[dict]) -> dict:
    """Compress a monthly projection into the headline figures the dashboard
    table / real workbook's own comparison table shows per contribution
    level, without re-deriving them from scratch."""
    if not rows:
        return {
            "investment_portion_monthly": 0.0, "total_invested": 0.0,
            "total_interest_credited": 0.0, "closing_balance": 0.0, "term_months": 0,
        }
    total_invested = round(rows[-1]["investment_portion"] * len(rows), 2)
    closing_balance = rows[-1]["closing_balance"]
    return {
        "investment_portion_monthly": rows[-1]["investment_portion"],
        "total_invested": total_invested,
        "total_interest_credited": round(closing_balance - total_invested, 2),
        "closing_balance": closing_balance,
        "term_months": len(rows),
    }
