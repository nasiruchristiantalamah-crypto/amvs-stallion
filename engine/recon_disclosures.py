"""
================================================================================
RECON DISCLOSURES — IFRS 17 LRC / LIC roll-forward disclosure
================================================================================
What this file does:
    Builds the standard IFRS 17 para 100-105 liability roll-forward
    disclosure — opening balance -> insurance service result -> finance
    income/expense -> cash flows -> closing balance — split by LRC
    (excluding and including the loss component) and LIC (including risk
    adjustment), exactly matching the row/column layout of PIC's own real
    "Recon Disclosures - Gross" / "Recon Disclosures - RI" sheets in
    "PIC - Journals 2025.xlsx". Built from data engine/journals.py's
    compute_movements() already computes — no new actuarial calculation,
    just the standard disclosure assembly of numbers that already exist.

Known, documented simplification (read before trusting the loss-component
split in an onerous period):
    PIC's real model tracks "LRC excluding loss component" and "Loss
    component" as two independently non-negative running balances, added
    together for the total. AMVS's engine.ifrs17_nonlife.ClassLiability
    instead carries ONE signed lrc value (= upr - dac) that goes negative
    when onerous, with loss_component = max(0, -lrc) derived FROM it as a
    descriptive magnitude, not tracked as a separate balance. For a
    non-onerous class (the common case — PIC's own real book has none)
    this reconciles exactly: loss_component is 0 and lrc IS "LRC excluding
    loss component". For an onerous class, the two-column split below is
    an honest best-effort presentation, not a genuine independent
    roll-forward of two separately-tracked balances — see
    engine/ifrs17_nonlife.py's _build_class_liability() for how lrc and
    loss_component are actually derived.

Movement basis — same "day 1" caveat as engine/journals.py: without a
persisted prior period, every movement is first-time recognition, so
"Changes that relate to past service" is always 0 here (there is no prior
estimate to have changed relative to) — disclosed via zero rows, not
fabricated.
================================================================================
"""

from dataclasses import dataclass
from typing import Optional

from engine.ifrs17_nonlife import ClassLiability
from engine.journals import MovementSet


@dataclass
class ReconDisclosure:
    """
    One class of business's LRC/LIC roll-forward, four columns matching
    PIC's own real sheet exactly: LRC excl loss component, LRC loss
    component, LIC incl risk adjustment, Total (= sum of the three).
    """
    class_of_business:   str
    basis:                str   # "gross" or "ri"

    opening_lrc_excl_lc:    float
    opening_loss_component:   float
    opening_lic:                 float
    opening_total:                  float

    insurance_revenue:                 float
    incurred_claims_and_expenses:        float
    changes_past_service:                  float   # always 0 — see module docstring
    onerous_losses_and_reversals:            float
    acquisition_cashflow_amortisation:         float
    insurance_service_expenses_total:            float
    insurance_service_result:                      float
    finance_income_expense:                          float
    total_recognised_in_comprehensive_income:          float

    investment_components:                               float   # always 0 — not modelled separately from claims, see docstring

    premiums_received:                                     float
    other_charges:                                           float
    claims_and_expenses_paid:                                  float
    acquisition_cashflows_deducted:                              float
    total_cash_flows:                                              float
    outstanding_transferred_to_lic:                                  float   # always 0 — day-1 recognition, nothing has expired yet

    closing_lrc_excl_lc:                                               float
    closing_loss_component:                                              float
    closing_lic:                                                           float
    closing_total:                                                           float


def build_recon_disclosure(
    current:    ClassLiability,
    movements:  MovementSet,
    basis:       str,
    prior:        Optional[ClassLiability] = None,
) -> ReconDisclosure:
    """
    Build one class's Recon Disclosure from its ClassLiability (opening/
    closing point) and MovementSet (the period's movements — see
    engine.journals.compute_movements()). `current`/`prior` must be the
    SAME basis as `basis` — pass the "ri" ClassLiability + a set of RI
    movements for basis="ri", not the gross ones.
    """
    p_lrc = prior.lrc if prior else 0.0
    p_loss_component = prior.loss_component if prior else 0.0
    p_lic = prior.lic if prior else 0.0

    opening_lrc_excl_lc = round(p_lrc, 2)
    opening_loss_component = round(p_loss_component, 2)
    opening_lic = round(p_lic, 2)
    opening_total = round(opening_lrc_excl_lc + opening_loss_component + opening_lic, 2)

    insurance_revenue = movements.premium_earned
    incurred_claims_and_expenses = round(movements.ibnr_movement + movements.ocr_movement + movements.ulae_movement, 2)
    changes_past_service = 0.0   # no persisted prior estimate to have changed relative to — see module docstring
    onerous_losses_and_reversals = round(current.loss_component - opening_loss_component, 2)
    acquisition_cashflow_amortisation = movements.dac_movement
    insurance_service_expenses_total = round(
        incurred_claims_and_expenses + changes_past_service + onerous_losses_and_reversals + acquisition_cashflow_amortisation, 2
    )
    insurance_service_result = round(insurance_revenue - insurance_service_expenses_total, 2)
    finance_income_expense = round(-movements.effect_of_discounting_movement, 2)
    total_recognised_in_comprehensive_income = round(insurance_service_result + finance_income_expense, 2)

    investment_components = 0.0   # not modelled separately from claims cash flows — see module docstring

    premiums_received = movements.premium_written
    other_charges = 0.0
    claims_and_expenses_paid = round(-movements.claims_paid, 2)
    acquisition_cashflows_deducted = round(-movements.dac_movement, 2) if movements.dac_movement > 0 else 0.0
    total_cash_flows = round(premiums_received + other_charges + claims_and_expenses_paid + acquisition_cashflows_deducted, 2)
    outstanding_transferred_to_lic = 0.0   # day-1 recognition — nothing has expired to LIC yet

    closing_lrc_excl_lc = round(current.lrc, 2) if not current.is_onerous else 0.0
    closing_loss_component = round(current.loss_component, 2)
    closing_lic = round(current.lic, 2)
    closing_total = round(closing_lrc_excl_lc + closing_loss_component + closing_lic, 2)

    return ReconDisclosure(
        class_of_business=current.class_of_business, basis=basis,
        opening_lrc_excl_lc=opening_lrc_excl_lc, opening_loss_component=opening_loss_component,
        opening_lic=opening_lic, opening_total=opening_total,
        insurance_revenue=insurance_revenue, incurred_claims_and_expenses=incurred_claims_and_expenses,
        changes_past_service=changes_past_service, onerous_losses_and_reversals=onerous_losses_and_reversals,
        acquisition_cashflow_amortisation=acquisition_cashflow_amortisation,
        insurance_service_expenses_total=insurance_service_expenses_total,
        insurance_service_result=insurance_service_result, finance_income_expense=finance_income_expense,
        total_recognised_in_comprehensive_income=total_recognised_in_comprehensive_income,
        investment_components=investment_components,
        premiums_received=premiums_received, other_charges=other_charges,
        claims_and_expenses_paid=claims_and_expenses_paid, acquisition_cashflows_deducted=acquisition_cashflows_deducted,
        total_cash_flows=total_cash_flows, outstanding_transferred_to_lic=outstanding_transferred_to_lic,
        closing_lrc_excl_lc=closing_lrc_excl_lc, closing_loss_component=closing_loss_component,
        closing_lic=closing_lic, closing_total=closing_total,
    )
