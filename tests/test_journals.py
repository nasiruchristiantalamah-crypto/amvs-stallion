"""
================================================================================
NON-LIFE JOURNAL ENGINE VALIDATION
================================================================================
Validates engine/journals.py against real PIC-derived non-life PAA
statements: every journal entry must be a valid double-entry line (exactly
one of debit/credit nonzero, both non-negative), and the full journal must
balance (total debits == total credits) — the fundamental accounting
identity a broken posting rule would violate immediately.

Run with:
    cd amvs
    pytest tests/test_journals.py -s
================================================================================
"""

import pytest

from engine.ifrs17_nonlife import generate_nonlife_paa_statements
from engine.data_loader import load_paid_claims
from engine.journals import generate_nonlife_journal, CHART_OF_ACCOUNTS


@pytest.fixture(scope="module")
def journal():
    statements = generate_nonlife_paa_statements(verbose=False)
    paid = load_paid_claims()
    return generate_nonlife_journal(statements, paid, period="FY2025")


def test_journal_is_non_empty(journal):
    assert len(journal) > 0


def test_every_entry_is_single_sided(journal):
    """Each posted line has exactly one of debit/credit nonzero, never both, never neither."""
    for e in journal:
        sides = [e.debit > 0, e.credit > 0]
        assert sum(sides) == 1, f"{e.class_of_business}/{e.narrative}/{e.account_code}: not single-sided (Dr={e.debit}, Cr={e.credit})"
        assert e.debit >= 0 and e.credit >= 0, f"{e.narrative}: negative amount posted"


def test_all_account_codes_are_defined(journal):
    for e in journal:
        assert e.account_code in CHART_OF_ACCOUNTS, f"Unknown account code {e.account_code!r} in entry: {e.narrative}"
        assert e.account_name == CHART_OF_ACCOUNTS[e.account_code][0]


# ── Chart of accounts — must match PIC's own real "2PAALedgerMoveFile" exactly ─
# (read directly from "PIC PAA COA Gross Total 2025.xlsx" — see engine/journals.py's module docstring)
REAL_PIC_CHART_OF_ACCOUNTS = {
    "201": "PAA Insurance (LIC) - PVFCF",
    "202": "PAA Insurance (LIC) - Risk Adjustment",
    "203": "PAA Insurance LRC",
    "204": "P&L (PAA Insurance Expenses)",
    "205": "P&L (PAA Insurance Finance)",
    "206": "P&L (PAA Insurance Revenue)",
    "207": "P&L (PAA Reinsurance Finance)",
    "208": "P&L (PAA Reinsurance Service)",
    "209": "PAA Reinsurance (LIC) - PVFCF",
    "210": "PAA Reinsurance (LIC) - Risk Adjustment",
    "211": "PAA Reinsurance LRC",
    "212": "P&L (PAA OCI)",
    "213": "P&L (PAA Reinsurance OCI)",
    "400": "Cash",
}


def test_chart_of_accounts_matches_pic_exactly():
    assert set(CHART_OF_ACCOUNTS.keys()) == set(REAL_PIC_CHART_OF_ACCOUNTS.keys())
    for code, real_name in REAL_PIC_CHART_OF_ACCOUNTS.items():
        assert CHART_OF_ACCOUNTS[code][0] == real_name, f"Account {code}: {CHART_OF_ACCOUNTS[code][0]!r} != {real_name!r}"


def test_ri_entries_never_post_to_a_gross_account_code(journal):
    """Reinsurance has its own accounts (209-211, 207, 208) — never the
    same codes as Gross (201-206) — see engine/journals.py's module
    docstring for why this differs from an earlier version of this module."""
    gross_only_codes = {"201", "202", "203", "204", "205", "206"}
    for e in journal:
        if e.basis == "ri":
            assert e.account_code not in gross_only_codes, f"RI entry posted to a Gross-only code: {e}"


def test_gross_entries_never_post_to_a_ri_only_account_code(journal):
    ri_only_codes = {"209", "210", "211"}
    for e in journal:
        if e.basis == "gross":
            assert e.account_code not in ri_only_codes, f"Gross entry posted to a RI-only code: {e}"


def test_journal_balances_in_total(journal):
    total_debit  = round(sum(e.debit for e in journal), 2)
    total_credit = round(sum(e.credit for e in journal), 2)
    print(f"\nTotal debit=GHS {total_debit:,.2f}  Total credit=GHS {total_credit:,.2f}")
    assert total_debit == total_credit


def test_each_movement_pair_balances(journal):
    """
    Every movement is posted as exactly 2 lines (one Dr, one Cr) with the
    same narrative — each such pair must balance on its own, not just in
    aggregate (catches a bug where two unrelated movements happen to net
    to zero by coincidence).
    """
    by_narrative = {}
    for e in journal:
        by_narrative.setdefault((e.class_of_business, e.narrative), []).append(e)

    for key, entries in by_narrative.items():
        assert len(entries) == 2, f"{key}: expected exactly 2 lines, found {len(entries)}"
        pair_debit  = sum(e.debit for e in entries)
        pair_credit = sum(e.credit for e in entries)
        assert abs(pair_debit - pair_credit) < 0.01, f"{key}: pair doesn't balance (Dr={pair_debit}, Cr={pair_credit})"


def test_premium_written_matches_upr(journal):
    """Day-1 recognition: premium written for a class should equal its gross UPR exactly."""
    statements = generate_nonlife_paa_statements(verbose=False)
    for cls in statements["classes"]:
        expected_upr = statements["by_class"][cls]["gross"].upr
        entries = [e for e in journal if e.class_of_business == cls and e.narrative == "Premium received / Cash inflow"]
        if expected_upr < 0.01:
            assert entries == []
            continue
        assert len(entries) == 2
        posted_amount = max(e.debit for e in entries)
        assert abs(posted_amount - expected_upr) < 0.01
