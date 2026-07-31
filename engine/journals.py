"""
================================================================================
NON-LIFE IFRS 17 JOURNAL ENTRIES
================================================================================
What this file does:
    Produces double-entry journal entries for every non-life IFRS 17
    movement in a period, from engine/ifrs17_nonlife.py's statement output:
    premium written, premium earned, claims paid, IBNR movement, OCR
    movement, ULAE movement, risk adjustment movement, effect-of-discounting
    movement, UPR movement, DAC movement, and RI recoverable/ceded
    movements — one Dr/Cr pair per movement, per class of business.

Chart of accounts:
    Based on PIC's own "2PAALedgerMoveFile" / "General_Ledger_PAA_Gross"
    structure in PIC PAA COA Gross Total 2025.xlsx (Primary Account codes
    for PAA Insurance LIC-PVFCF (201), LIC-Risk Adjustment (202), LRC
    (203), P&L expense/finance/revenue accounts (204-207), Cash (400)),
    extended with accounts PIC's own COA dump didn't cover but this
    engine's statement structure needs (a separate ULAE account, an
    explicit Effect-of-Discounting contra account, DAC as its own asset
    account, and Reinsurance LIC/LRC accounts) — see CHART_OF_ACCOUNTS
    below for the full code list and which are PIC-sourced vs extended.

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

from dataclasses import dataclass, field
from datetime import date as date_type
from typing import Dict, List, Optional

from engine.ifrs17_nonlife import ClassLiability

# ── Chart of accounts ────────────────────────────────────────────────────────
# code: (name, account_type)  -- account_type is "asset", "liability", or "pnl"
CHART_OF_ACCOUNTS: Dict[str, tuple] = {
    # Assets
    "101": ("Cash", "asset"),
    "102": ("Deferred Acquisition Costs (DAC)", "asset"),
    "103": ("Reinsurance Recoverable — LIC Best Estimate", "asset"),   # PIC-style: PAA Reinsurance (LIC) - PVFCF
    "104": ("Reinsurance Recoverable — Risk Adjustment", "asset"),
    # Liabilities (PIC codes 201-203 carried through directly; 204+ extended)
    "201": ("PAA Insurance LIC — Best Estimate (IBNR + OCR)", "liability"),
    "202": ("PAA Insurance LIC — Risk Adjustment", "liability"),
    "203": ("PAA Insurance LRC — Unearned Premium Reserve", "liability"),
    "204": ("PAA Insurance LIC — ULAE", "liability"),
    "205": ("PAA Reinsurance LRC — Unearned Premium Ceded", "liability"),   # contra, reduces net LRC
    # P&L
    "301": ("P&L — Insurance Revenue (Premium Earned)", "pnl"),
    "302": ("P&L — Insurance Service Expense (Claims Incurred)", "pnl"),
    "303": ("P&L — Insurance Service Expense (ULAE)", "pnl"),
    "304": ("P&L — Insurance Service Expense (Acquisition Costs)", "pnl"),
    "305": ("P&L — Insurance Finance Income (Effect of Discounting)", "pnl"),
    "306": ("P&L — Reinsurance Expense (Premium Ceded)", "pnl"),
    "307": ("P&L — Reinsurance Recoveries", "pnl"),
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
    basis:               str    # "gross" or "ri"
    period:               str


@dataclass
class MovementSet:
    """One class of business's period movements, ready to post."""
    class_of_business:      str
    premium_written:         float
    premium_earned:            float
    claims_paid:                  float
    ibnr_movement:                  float
    ocr_movement:                      float
    ulae_movement:                       float
    risk_adjustment_movement:               float
    effect_of_discounting_movement:            float
    dac_movement:                                 float
    ri_recoverable_best_estimate_movement:          float
    ri_recoverable_ra_movement:                        float
    ri_ceded_premium_movement:                            float


def compute_movements(
    current:        ClassLiability,
    paid_claims:    float,
    prior:           Optional[ClassLiability] = None,
    prior_ri:         Optional[ClassLiability] = None,
) -> MovementSet:
    """
    Compute this period's movements for one class of business.

    Parameters:
        current       : this period's gross ClassLiability (from
                          engine.ifrs17_nonlife.generate_nonlife_paa_statements)
        paid_claims   : actual cash claims paid this period, gross
                          (engine.data_loader.load_paid_claims())
        prior          : the SAME class's gross ClassLiability at the prior
                          period's close, or None for first-time
                          ("day 1") recognition — see module docstring.
        prior_ri        : same, for the RI (ceded) basis — pass alongside
                          `prior` when doing a genuine period-over-period
                          roll-forward; `current` here must be the GROSS
                          ClassLiability, RI figures come from the
                          statement's own "ri" basis entry for this class.

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
        claims_paid                              = round(paid_claims, 2),
        ibnr_movement                            = round(delta(current.ibnr, prior.ibnr if prior else None), 2),
        ocr_movement                             = round(delta(current.ocr, prior.ocr if prior else None), 2),
        ulae_movement                            = round(delta(current.ulae, prior.ulae if prior else None), 2),
        risk_adjustment_movement                 = round(delta(current.risk_adjustment, prior.risk_adjustment if prior else None), 2),
        effect_of_discounting_movement           = round(delta(current.effect_of_discounting, prior.effect_of_discounting if prior else None), 2),
        dac_movement                             = round(delta(current.dac, p_dac), 2),
        ri_recoverable_best_estimate_movement    = 0.0,   # set by caller from the "ri" basis — see generate_nonlife_journal
        ri_recoverable_ra_movement               = 0.0,
        ri_ceded_premium_movement                = 0.0,
    )


def post_journal_entries(
    movements:    MovementSet,
    period:        str,
    posting_date:   Optional[str] = None,
) -> List[JournalEntry]:
    """
    Turn one class's MovementSet into balanced double-entry journal lines.
    Every movement posts exactly one debit and one credit of equal amount;
    a zero movement is skipped (no point posting a no-op entry).
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

    # Premium written: Dr Cash, Cr LRC (Unearned Premium)
    post("gross", "101", "203", movements.premium_written, f"{cls}: premium written")
    # Premium earned: Dr LRC (Unearned Premium), Cr Insurance Revenue
    post("gross", "203", "301", movements.premium_earned, f"{cls}: premium earned")
    # Claims paid: Dr LIC (Best Estimate), Cr Cash
    post("gross", "201", "101", movements.claims_paid, f"{cls}: claims paid")
    # IBNR / OCR movement: Dr Insurance Service Expense, Cr LIC (Best Estimate)
    post("gross", "302", "201", movements.ibnr_movement, f"{cls}: IBNR movement")
    post("gross", "302", "201", movements.ocr_movement, f"{cls}: OCR movement")
    # ULAE movement: Dr Insurance Service Expense (ULAE), Cr LIC (ULAE)
    post("gross", "303", "204", movements.ulae_movement, f"{cls}: ULAE movement")
    # Risk adjustment movement: Dr Insurance Service Expense, Cr LIC (Risk Adjustment)
    post("gross", "302", "202", movements.risk_adjustment_movement, f"{cls}: risk adjustment movement")
    # Effect of discounting: reduces the liability, recognised as finance income.
    # effect_of_discounting_movement is <= 0 (see ifrs17_nonlife.py), so this
    # naturally posts Dr LIC (reducing it) / Cr Finance Income.
    post("gross", "201", "305", -movements.effect_of_discounting_movement, f"{cls}: effect of discounting")
    # DAC movement: Dr DAC (asset), Cr Insurance Service Expense (Acquisition
    # Costs) — capitalising the acquisition cost defers it out of this period's expense.
    post("gross", "102", "304", movements.dac_movement, f"{cls}: DAC movement")

    # RI recoverable movements
    post("ri", "103", "307", movements.ri_recoverable_best_estimate_movement, f"{cls}: RI recoverable — best estimate")
    post("ri", "104", "307", movements.ri_recoverable_ra_movement, f"{cls}: RI recoverable — risk adjustment")
    # RI ceded premium: Dr Reinsurance Expense, Cr Reinsurance LRC (Unearned Premium Ceded)
    post("ri", "306", "205", movements.ri_ceded_premium_movement, f"{cls}: RI ceded premium")

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
            ri_ceded_prem_movement = current_ri.lrc
        else:
            ri_be_movement = current_ri_best_estimate - (prior_ri.ibnr + prior_ri.ocr)
            ri_ra_movement = current_ri.risk_adjustment - prior_ri.risk_adjustment
            ri_ceded_prem_movement = current_ri.lrc - prior_ri.lrc

        movements.ri_recoverable_best_estimate_movement = round(ri_be_movement, 2)
        movements.ri_recoverable_ra_movement = round(ri_ra_movement, 2)
        movements.ri_ceded_premium_movement  = round(ri_ceded_prem_movement, 2)

        all_entries.extend(post_journal_entries(movements, period, posting_date))

    return all_entries
