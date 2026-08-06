"""
================================================================================
GENERAL LEDGER — T-account roll-forward from posted journal entries
================================================================================
What this file does:
    Rolls engine/journals.py's flat list of JournalEntry postings up into
    a genuine T-account per primary account: opening balance, total
    debits, total credits, closing balance — the missing piece between
    "journal entries exist" (they already do — see that module) and "a
    general ledger exists". This is assembly, not new actuarial logic:
    journals.py already posts balanced Dr/Cr pairs per movement, this file
    just aggregates them per account.

Sign convention:
    Every account's balance is carried DEBIT-SIGNED throughout — positive
    means a net debit position, negative means a net credit position,
    uniformly across asset/liability/pnl accounts:
        closing_balance = opening_balance + total_debits - total_credits
    This is the standard trial-balance convention (real accounting
    software works the same way) and avoids needing to special-case each
    account's "normal" debit/credit direction just to compute a correct
    running balance. Whether a positive number matches the account's
    EXPECTED sign (assets/expenses normally run positive; liabilities/
    revenue normally run negative under this convention) is a
    presentation concern for a caller, not something this module needs to
    know to be correct.

Fundamental check — the trial balance:
    Since journals.py's post_journal_entries() posts exactly one debit and
    one equal credit per movement, summing every account's closing
    balance across the WHOLE ledger must always be exactly zero — this is
    the textbook definition of a balanced trial balance, and the
    strongest possible sanity check that nothing was posted one-sided.
    See trial_balance_is_zero() below.

What this does NOT do:
    Persist balances across periods. build_general_ledger() takes
    opening_balances as a plain parameter (mirroring journals.py's own
    compute_movements(prior=...) pattern) — it doesn't read or write
    anything itself. A caller doing genuine period-over-period reporting
    supplies the prior period's closing balances (see closing_balances()
    below for the exact shape to chain forward, e.g. via a
    rollforward_store.py-style snapshot) — that wiring is a separate,
    later step, not part of this module.
================================================================================
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from engine.journals import CHART_OF_ACCOUNTS, JournalEntry


@dataclass
class LedgerAccount:
    """One primary account's T-account for a single posting run — debit-signed throughout, see module docstring."""
    account_code:      str
    account_name:      str
    account_type:      str    # "asset", "liability", or "pnl" — see CHART_OF_ACCOUNTS
    opening_balance:   float
    total_debits:      float
    total_credits:     float
    closing_balance:   float
    entry_count:       int


def build_general_ledger(
    entries:            List[JournalEntry],
    opening_balances:   Optional[Dict[str, float]] = None,
) -> Dict[str, LedgerAccount]:
    """
    Roll a flat list of posted journal entries up into one T-account per
    primary account code touched by any entry.

    Parameters:
        entries           : engine.journals.generate_nonlife_journal() output
                             (or any list of JournalEntry — not restricted
                             to a single period/class/basis, so a caller
                             can pass everything for a full-portfolio
                             ledger or pre-filter for a narrower view)
        opening_balances  : {account_code: debit-signed opening balance},
                             from the prior period's closing (see module
                             docstring) — omit entirely for first-time
                             ("day 1") recognition, same convention
                             engine.journals.compute_movements() uses for
                             prior=None.

    Returns:
        {account_code: LedgerAccount}, one entry per account CODE actually
        touched by `entries` — an account never posted to in this run
        simply doesn't appear, rather than showing a zero-activity row.
    """
    opening_balances = opening_balances or {}
    totals: Dict[str, Dict[str, float]] = {}

    for e in entries:
        acc = totals.setdefault(e.account_code, {"debits": 0.0, "credits": 0.0, "count": 0})
        acc["debits"]  += e.debit
        acc["credits"] += e.credit
        acc["count"]   += 1

    ledger: Dict[str, LedgerAccount] = {}
    for code, acc in totals.items():
        name, account_type = CHART_OF_ACCOUNTS.get(code, (f"Unknown account {code}", "unknown"))
        opening = opening_balances.get(code, 0.0)
        closing = opening + acc["debits"] - acc["credits"]
        ledger[code] = LedgerAccount(
            account_code=code, account_name=name, account_type=account_type,
            opening_balance=round(opening, 2), total_debits=round(acc["debits"], 2),
            total_credits=round(acc["credits"], 2), closing_balance=round(closing, 2),
            entry_count=acc["count"],
        )
    return ledger


def closing_balances(ledger: Dict[str, LedgerAccount]) -> Dict[str, float]:
    """Convenience extractor — exactly the shape build_general_ledger()'s own
    opening_balances parameter expects, for chaining into the next period."""
    return {code: acc.closing_balance for code, acc in ledger.items()}


def trial_balance_is_zero(ledger: Dict[str, LedgerAccount], tolerance: float = 0.01) -> bool:
    """
    The fundamental double-entry invariant: every account's closing
    balance, summed across the whole ledger, must net to zero — every
    posted entry contributed an equal, offsetting debit and credit
    somewhere. A non-zero total means something in the posting pipeline
    broke that guarantee (a one-sided post), not a legitimate accounting
    outcome — this should never fail on real output.
    """
    return abs(sum(acc.closing_balance for acc in ledger.values())) < tolerance


def general_ledger_by_class(
    entries:            List[JournalEntry],
    opening_balances:   Optional[Dict[str, Dict[str, float]]] = None,
) -> Dict[str, Dict[str, LedgerAccount]]:
    """
    Same as build_general_ledger(), but broken out per class of business —
    {class_of_business: {account_code: LedgerAccount}} — for a class-level
    (not just whole-portfolio) T-account view. Gross and RI basis entries
    are naturally kept apart already (they post to entirely different
    account codes — see journals.py's CHART_OF_ACCOUNTS), so no separate
    Gross/RI split is needed on top of this.

    opening_balances, if supplied, is keyed the same way:
    {class_of_business: {account_code: balance}}.
    """
    opening_balances = opening_balances or {}
    by_class: Dict[str, List[JournalEntry]] = {}
    for e in entries:
        by_class.setdefault(e.class_of_business, []).append(e)

    return {
        cls: build_general_ledger(cls_entries, opening_balances.get(cls))
        for cls, cls_entries in by_class.items()
    }
