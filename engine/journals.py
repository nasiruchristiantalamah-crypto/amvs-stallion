"""
================================================================================
NON-LIFE IFRS 17 JOURNAL ENTRIES
================================================================================
What this file does:
    Produces double-entry journal entries for every non-life IFRS 17
    movement in a period, from engine/ifrs17_nonlife.py's statement output:
    premium written, premium earned, claims paid, IBNR movement, OCR
    movement, ULAE movement, risk adjustment movement, effect-of-discounting
    movement, DAC/acquisition cost movement, and RI recoverable/ceded
    movements — one Dr/Cr pair per movement, per class of business.

Chart of accounts — reconciled against PIC's own real ledger output:
    Read directly from PIC's real "PIC PAA COA Gross Total 2025.xlsx",
    sheet 2PAALedgerMoveFile (the actual movement-mapping template PIC's
    own actuarial modelling tool — GREEN13/Iris-Data — uses) and
    cross-checked against the General_Ledger_PAA_* T-account sheets in the
    same workbook. 14 accounts, EXACTLY as PIC's own template names and
    codes them — see CHART_OF_ACCOUNTS below. Two structural facts that
    differ from an earlier version of this module, both confirmed against
    the real file:
      - Reinsurance gets its OWN account codes (209-211, 207, 208), not
        the same 201-203 codes as Gross with a "ri" flag distinguishing
        them. A gross and a ceded movement are never the same account.
      - There is no separate DAC or ULAE account in PIC's real chart.
        DAC nets directly into "PAA Insurance LRC" (203) — confirmed by
        engine.ifrs17_nonlife.ClassLiability.lrc already being upr - dac,
        i.e. AMVS's own model already carries LRC net of DAC, matching
        PIC's presentation exactly. ULAE has no account of its own either
        (PIC's real balance sheet doesn't disclose ULAE as a separate
        line) — its P&L/liability impact posts through the same accounts
        as IBNR/OCR (204 / 201).

    Known, documented simplification vs PIC's real 74-movement template:
    PIC's own ledger splits some of these movements more finely than this
    engine currently computes — e.g. "interest accretion on LIC [PVFCF]"
    and "effect of changes in interest rates and other financial
    assumptions" are two separate real movements, both against the same
    (201, 205) account pair, but engine.ifrs17_nonlife.py only produces
    ONE combined effect_of_discounting figure today. Where this module
    only has one combined number, it posts one combined line rather than
    fabricating a split the underlying engine doesn't actually compute —
    the ACCOUNT CODES and movement NARRATIVES below are matched to PIC's
    real template either way, only the sub-line granularity differs.

Movement basis — IMPORTANT, read before interpreting the output:
    engine/ifrs17_nonlife.py produces a POINT-IN-TIME snapshot (this
    period's closing balances), not a period-over-period roll-forward like
    the life side's engine/ifrs17.py. Without a prior period's balances to
    diff against, compute_movements() treats the ENTIRE current balance as
    a first-time recognition (as if the whole in-force book were written
    this period — "day 1" movements). Pass `prior` (another
    engine.ifrs17_nonlife.ClassLiability for the same class) to
    compute_movements() to get genuine period-over-period movements
    instead — the arithmetic is identical either way, only the "opening"
    reference point changes.
================================================================================
"""

from dataclasses import dataclass
from datetime import date as date_type
from typing import Dict, List, Optional

from engine.ifrs17_nonlife import ClassLiability

# ── Chart of accounts ────────────────────────────────────────────────────────
# code: (name, account_type) -- account_type is "asset", "liability", or "pnl".
# Every code/name here is PIC's own, read verbatim from their real
# 2PAALedgerMoveFile — see module docstring.
CHART_OF_ACCOUNTS: Dict[str, tuple] = {
    "201": ("PAA Insurance (LIC) - PVFCF", "liability"),
    "202": ("PAA Insurance (LIC) - Risk Adjustment", "liability"),
    "203": ("PAA Insurance LRC", "liability"),
    "204": ("P&L (PAA Insurance Expenses)", "pnl"),
    "205": ("P&L (PAA Insurance Finance)", "pnl"),
    "206": ("P&L (PAA Insurance Revenue)", "pnl"),
    "207": ("P&L (PAA Reinsurance Finance)", "pnl"),
    "208": ("P&L (PAA Reinsurance Service)", "pnl"),
    "209": ("PAA Reinsurance (LIC) - PVFCF", "asset"),
    "210": ("PAA Reinsurance (LIC) - Risk Adjustment", "asset"),
    "211": ("PAA Reinsurance LRC", "asset"),
    "212": ("P&L (PAA OCI)", "pnl"),
    "213": ("P&L (PAA Reinsurance OCI)", "pnl"),
    "400": ("Cash", "asset"),
}


@dataclass
class JournalEntry:
    date:                str
    account_code:        str
    account_name:        str
    debit:               float
    credit:              float
    narrative:           str
    class_of_business:   str
    basis:               str    # "gross" or "ri" — every RI line already posts to its own 209-213/207/208 codes; this is a convenience filter, not what makes the codes correct
    period:               str


@dataclass
class MovementSet:
    """One class of business's period movements, ready to post."""
    class_of_business:            str
    premium_written:               float
    premium_earned:                 float
    dac_movement:                    float
    claims_paid:                      float
    ibnr_movement:                     float
    ocr_movement:                        float
    ulae_movement:                        float
    risk_adjustment_movement:              float
    effect_of_discounting_movement:          float
    ri_recoverable_best_estimate_movement:     float
    ri_recoverable_ra_movement:                  float
    ri_effect_of_discounting_movement:            float
    ri_ceded_premium_movement:                      float


def compute_movements(
    current:        ClassLiability,
    paid_claims:    float,
    prior:           Optional[ClassLiability] = None,
) -> MovementSet:
    """
    Compute this period's GROSS movements for one class of business. RI
    movements are computed separately in generate_nonlife_journal() (they
    come from the statement's own "ri" basis entry, not this function).

    Parameters:
        current       : this period's gross ClassLiability (from
                          engine.ifrs17_nonlife.generate_nonlife_paa_statements)
        paid_claims   : actual cash claims paid this period, gross
                          (engine.data_loader.load_paid_claims())
        prior          : the SAME class's gross ClassLiability at the prior
                          period's close, or None for first-time
                          ("day 1") recognition — see module docstring.

    Returns:
        MovementSet ready for post_journal_entries().
    """
    def delta(curr_val: float, prior_val: Optional[float]) -> float:
        return curr_val if prior_val is None else curr_val - prior_val

    p_upr = prior.upr if prior else None
    p_dac = prior.dac if prior else None

    upr_movement = delta(current.upr, p_upr)
    # Premium earned = the portion of the UPR movement attributable to
    # coverage already provided; premium written is whatever's left after
    # netting off against the UPR movement (written - earned = UPR movement).
    # With no prior period, nothing has been earned yet (day 1): the whole
    # premium is unearned, so premium_written = UPR, premium_earned = 0.
    if prior is None:
        premium_written = current.upr
        premium_earned  = 0.0
    else:
        premium_written = max(0.0, upr_movement) if upr_movement >= 0 else 0.0
        premium_earned  = premium_written - upr_movement

    return MovementSet(
        class_of_business                       = current.class_of_business,
        premium_written                          = round(premium_written, 2),
        premium_earned                           = round(premium_earned, 2),
        dac_movement                             = round(delta(current.dac, p_dac), 2),
        claims_paid                              = round(paid_claims, 2),
        ibnr_movement                            = round(delta(current.ibnr, prior.ibnr if prior else None), 2),
        ocr_movement                             = round(delta(current.ocr, prior.ocr if prior else None), 2),
        ulae_movement                            = round(delta(current.ulae, prior.ulae if prior else None), 2),
        risk_adjustment_movement                 = round(delta(current.risk_adjustment, prior.risk_adjustment if prior else None), 2),
        effect_of_discounting_movement           = round(delta(current.effect_of_discounting, prior.effect_of_discounting if prior else None), 2),
        ri_recoverable_best_estimate_movement    = 0.0,   # set by caller from the "ri" basis — see generate_nonlife_journal
        ri_recoverable_ra_movement               = 0.0,
        ri_effect_of_discounting_movement        = 0.0,
        ri_ceded_premium_movement                = 0.0,
    )


def post_journal_entries(
    movements:    MovementSet,
    period:        str,
    posting_date:   Optional[str] = None,
) -> List[JournalEntry]:
    """
    Turn one class's MovementSet into balanced double-entry journal lines,
    account codes and narratives matched to PIC's own real chart of
    accounts (see module docstring). Every movement posts exactly one
    debit and one credit of equal amount; a zero movement is skipped (no
    point posting a no-op entry).
    """
    posting_date = posting_date or date_type.today().isoformat()
    cls = movements.class_of_business
    entries: List[JournalEntry] = []

    def post(basis: str, debit_code: str, credit_code: str, amount: float, narrative: str) -> None:
        if abs(amount) < 0.005:
            return
        amt = abs(amount)
        dr_code, cr_code = (debit_code, credit_code) if amount >= 0 else (credit_code, debit_code)
        for code, dr, cr in ((dr_code, amt, 0.0), (cr_code, 0.0, amt)):
            name, _ = CHART_OF_ACCOUNTS[code]
            entries.append(JournalEntry(
                date=posting_date, account_code=code, account_name=name,
                debit=round(dr, 2), credit=round(cr, 2),
                narrative=narrative, class_of_business=cls, basis=basis, period=period,
            ))

    # Premium received / cash inflow: Dr Cash (400), Cr PAA Insurance LRC (203)
    post("gross", "400", "203", movements.premium_written, "Premium received / Cash inflow")
    # Subsequent measurement — release of the LRC to revenue: Dr LRC (203), Cr Insurance Revenue (206)
    post("gross", "203", "206", movements.premium_earned, "Subsequent measurement [Release of the LRC to revenue]")
    # Recognition of acquisition cost allocated to the period: Dr Insurance Expenses (204), Cr LRC (203)
    # — LRC is already carried net of DAC (ClassLiability.lrc = upr - dac), matching
    # PIC's own presentation, so this is the one movement DAC needs against 203/204.
    post("gross", "204", "203", movements.dac_movement, "Recognition of acquisition cost allocated to the period")
    # Payment of claims: Dr LIC-PVFCF (201), Cr Cash (400)
    post("gross", "201", "400", movements.claims_paid, "Payment of Claims")
    # Recognition of claims incurred in the period [current service cost]: Dr Insurance Expenses (204), Cr LIC-PVFCF (201)
    post("gross", "204", "201", movements.ibnr_movement, "Recognition of claims incurred in the period [current service cost] — IBNR")
    post("gross", "204", "201", movements.ocr_movement, "Recognition of claims incurred in the period [current service cost] — OCR")
    # ULAE has no account of its own in PIC's real chart — its P&L/liability
    # impact runs through the same pair as IBNR/OCR (see module docstring).
    post("gross", "204", "201", movements.ulae_movement, "Recognition of claims handling expenses (ULAE)")
    # Release of Risk Adjustment: Dr Insurance Expenses (204), Cr LIC-Risk Adjustment (202)
    post("gross", "204", "202", movements.risk_adjustment_movement, "Release of Risk Adjustment")
    # Interest accretion on LIC [Best estimate liability]: Dr LIC-PVFCF (201), Cr Insurance Finance (205)
    # effect_of_discounting_movement is <= 0 (see ifrs17_nonlife.py), so this
    # naturally posts Dr LIC-PVFCF (reducing it) / Cr Insurance Finance.
    post("gross", "201", "205", -movements.effect_of_discounting_movement, "Interest accretion on LIC [Best estimate liability]")

    # RI recoverable movements — Reinsurance's own accounts (209-211, 207, 208), never the Gross codes.
    post("ri", "209", "208", movements.ri_recoverable_best_estimate_movement, "Changes / Increase in recoverable amounts - adjustments to LIC (PVFC)")
    post("ri", "210", "208", movements.ri_recoverable_ra_movement, "Changes / Increase in recoverable amounts - adjustments to LIC (RA)")
    post("ri", "209", "207", -movements.ri_effect_of_discounting_movement, "Interest accretion on Reinsurance LIC [Best estimate liability]")
    # Net movement in the ceded LRC asset (Reinsurance LRC, 211) — a single
    # combined figure (see module docstring: PIC splits "premium paid" from
    # "asset released to P&L" as two movements; this engine has one net number).
    post("ri", "211", "208", movements.ri_ceded_premium_movement, "Release of the reinsurance asset (Premium)")

    return entries


def generate_nonlife_journal(
    statements:     dict,
    paid_claims:      Dict[str, float],
    period:            str,
    posting_date:        Optional[str] = None,
    prior_statements:      Optional[dict] = None,
) -> List[JournalEntry]:
    """
    Top-level entry point: build the full journal for every class of
    business in a non-life PAA statement set.

    Parameters:
        statements         : engine.ifrs17_nonlife.generate_nonlife_paa_statements() output
        paid_claims        : {class: gross paid claims this period} —
                               engine.data_loader.load_paid_claims()
        period              : reporting period label, e.g. "FY2025"
        posting_date        : journal date (defaults to today)
        prior_statements    : same shape as `statements`, from the prior
                               period, for genuine period-over-period
                               movements — omit for first-time ("day 1")
                               recognition (see module docstring)

    Returns:
        List[JournalEntry] covering every class of business, gross and RI.
    """
    all_entries: List[JournalEntry] = []

    for cls in statements["classes"]:
        current_gross = statements["by_class"][cls]["gross"]
        current_ri    = statements["by_class"][cls]["ri"]

        prior_gross = prior_statements["by_class"][cls]["gross"] if prior_statements else None
        prior_ri    = prior_statements["by_class"][cls]["ri"]    if prior_statements else None

        movements = compute_movements(current_gross, paid_claims.get(cls, 0.0), prior_gross)

        # RI movements come from the "ri" basis's own figures (already
        # Gross - Net from run_nic_summary), same first-time-vs-prior logic.
        current_ri_best_estimate = current_ri.ibnr + current_ri.ocr
        if prior_ri is None:
            ri_be_movement = current_ri_best_estimate
            ri_ra_movement = current_ri.risk_adjustment
            ri_discounting_movement = current_ri.effect_of_discounting
            ri_ceded_prem_movement = current_ri.lrc
        else:
            ri_be_movement = current_ri_best_estimate - (prior_ri.ibnr + prior_ri.ocr)
            ri_ra_movement = current_ri.risk_adjustment - prior_ri.risk_adjustment
            ri_discounting_movement = current_ri.effect_of_discounting - prior_ri.effect_of_discounting
            ri_ceded_prem_movement = current_ri.lrc - prior_ri.lrc

        movements.ri_recoverable_best_estimate_movement = round(ri_be_movement, 2)
        movements.ri_recoverable_ra_movement = round(ri_ra_movement, 2)
        movements.ri_effect_of_discounting_movement = round(ri_discounting_movement, 2)
        movements.ri_ceded_premium_movement  = round(ri_ceded_prem_movement, 2)

        all_entries.extend(post_journal_entries(movements, period, posting_date))

    return all_entries
