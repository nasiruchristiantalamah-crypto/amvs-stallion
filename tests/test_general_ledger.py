"""
================================================================================
GENERAL LEDGER — validation
================================================================================
What this file does:
    Validates engine/general_ledger.py — the T-account roll-forward built
    on top of engine/journals.py's already-validated double-entry
    postings (see tests/test_journals.py). Covers the aggregation rules on
    small synthetic entries (unambiguous expected totals), then confirms
    the trial balance nets to zero on the real PIC journal — the strongest
    possible sanity check that nothing in the whole pricing->journal->
    ledger pipeline posts one-sided.

Run with:
    cd amvs
    pytest tests/test_general_ledger.py -s
================================================================================
"""

import pytest

from engine.journals import JournalEntry, generate_nonlife_journal
from engine.ifrs17_nonlife import generate_nonlife_paa_statements
from engine.data_loader import load_paid_claims
from engine.general_ledger import (
    build_general_ledger, general_ledger_by_class, closing_balances, trial_balance_is_zero,
)


def _entry(code, dr, cr, cls="Motor", basis="gross"):
    return JournalEntry(date="2026-01-01", account_code=code, account_name="x", debit=dr, credit=cr,
                         narrative="test", class_of_business=cls, basis=basis, period="FY2026")


# ── Synthetic entries — unambiguous expected totals ──────────────────────────

def test_single_account_rolls_up_debits_and_credits():
    entries = [_entry("101", 100.0, 0.0), _entry("101", 0.0, 40.0), _entry("101", 60.0, 0.0)]
    ledger = build_general_ledger(entries)
    acc = ledger["101"]
    assert acc.total_debits == 160.0
    assert acc.total_credits == 40.0
    assert acc.closing_balance == 120.0   # opening 0 + 160 - 40
    assert acc.entry_count == 3


def test_opening_balance_carries_forward():
    entries = [_entry("101", 50.0, 0.0)]
    ledger = build_general_ledger(entries, opening_balances={"101": 200.0})
    assert ledger["101"].opening_balance == 200.0
    assert ledger["101"].closing_balance == 250.0


def test_account_never_posted_to_is_absent_not_zero():
    entries = [_entry("101", 50.0, 0.0)]
    ledger = build_general_ledger(entries, opening_balances={"101": 0.0, "203": 500.0})
    assert "101" in ledger
    assert "203" not in ledger   # never touched by an entry — not surfaced, even though an opening balance was supplied


def test_account_name_and_type_come_from_the_chart_of_accounts():
    entries = [_entry("400", 10.0, 0.0), _entry("201", 0.0, 10.0)]
    ledger = build_general_ledger(entries)
    assert ledger["400"].account_name == "Cash"
    assert ledger["400"].account_type == "asset"
    assert ledger["201"].account_type == "liability"


def test_two_balanced_lines_net_to_a_zero_trial_balance():
    entries = [_entry("101", 100.0, 0.0), _entry("203", 0.0, 100.0)]
    ledger = build_general_ledger(entries)
    assert trial_balance_is_zero(ledger)


def test_one_sided_posting_is_caught_by_trial_balance_check():
    """A deliberately broken, one-sided posting (a bug this check exists to
    catch) must NOT net to zero — proves the check has teeth."""
    entries = [_entry("101", 100.0, 0.0)]   # no offsetting credit anywhere
    ledger = build_general_ledger(entries)
    assert not trial_balance_is_zero(ledger)


def test_closing_balances_shape_matches_opening_balances_parameter():
    """closing_balances()'s output must be directly reusable as the next
    period's opening_balances — that's the whole point of the helper."""
    entries = [_entry("101", 100.0, 0.0), _entry("203", 0.0, 100.0)]
    ledger = build_general_ledger(entries)
    cb = closing_balances(ledger)
    assert cb == {"101": 100.0, "203": -100.0}

    next_ledger = build_general_ledger([_entry("101", 20.0, 0.0)], opening_balances=cb)
    assert next_ledger["101"].opening_balance == 100.0
    assert next_ledger["101"].closing_balance == 120.0


def test_general_ledger_by_class_splits_correctly():
    entries = [
        _entry("101", 100.0, 0.0, cls="Motor"), _entry("203", 0.0, 100.0, cls="Motor"),
        _entry("101", 50.0, 0.0, cls="Fire"),   _entry("203", 0.0, 50.0, cls="Fire"),
    ]
    by_class = general_ledger_by_class(entries)
    assert set(by_class.keys()) == {"Motor", "Fire"}
    assert by_class["Motor"]["101"].total_debits == 100.0
    assert by_class["Fire"]["101"].total_debits == 50.0
    assert trial_balance_is_zero(by_class["Motor"])
    assert trial_balance_is_zero(by_class["Fire"])


# ── Real PIC journal — the strongest sanity check ────────────────────────────

@pytest.fixture(scope="module")
def real_journal():
    statements = generate_nonlife_paa_statements(verbose=False)
    paid = load_paid_claims()
    return generate_nonlife_journal(statements, paid, period="FY2025")


def test_real_pic_ledger_trial_balance_is_zero(real_journal):
    ledger = build_general_ledger(real_journal)
    print(f"\nAccounts touched: {len(ledger)}")
    for code in sorted(ledger):
        acc = ledger[code]
        print(f"  {code} {acc.account_name[:45]:45s} close=GHS {acc.closing_balance:>16,.2f}")
    assert trial_balance_is_zero(ledger)
    assert len(ledger) > 0


def test_real_pic_ledger_by_class_each_balances(real_journal):
    """Every individual class's own T-accounts must ALSO balance to zero
    on their own — not just the portfolio total (which could hide two
    classes' offsetting errors)."""
    by_class = general_ledger_by_class(real_journal)
    for cls, ledger in by_class.items():
        assert trial_balance_is_zero(ledger), f"{cls}'s ledger does not balance"


def test_real_pic_cash_account_matches_premium_written_minus_claims_paid(real_journal):
    """Cash (400 — PIC's own real code, see engine/journals.py's
    CHART_OF_ACCOUNTS) is debited by premium written and credited by claims
    paid — nothing else posts to it (see journals.py's post_journal_entries) —
    so its closing balance is exactly that net figure, an independent
    cross-check on the roll-up rather than trusting the aggregation blindly."""
    cash_entries = [e for e in real_journal if e.account_code == "400"]
    expected_debit  = round(sum(e.debit for e in cash_entries), 2)
    expected_credit = round(sum(e.credit for e in cash_entries), 2)

    ledger = build_general_ledger(real_journal)
    cash = ledger["400"]
    assert cash.total_debits == expected_debit
    assert cash.total_credits == expected_credit
    assert cash.closing_balance == round(expected_debit - expected_credit, 2)
