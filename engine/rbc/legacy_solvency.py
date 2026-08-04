"""
================================================================================
LEGACY SOLVENCY MARGIN — pre-GIRBC transition-basis solvency test
================================================================================
What this file does:
    The older, simpler NIC solvency test Ghanaian non-life insurers still
    report as their headline CAR while GIRBC phases in (mandatory 1 Jan
    2027 — see clients/qic/assumptions.yaml's validation history). Unlike
    GIRBC's risk-module/correlation-matrix approach, this is a volume-based
    test:

        Required Capital = MAX(GHS 10,000,000, 25% x Net Written Premium, 25% x Management Expenses)
        Available Capital Resources = (Total Capital Base - Capital Deductions) - sum(asset_balance x haircut, by asset type)
        Legacy CAR = Available Capital Resources / Required Capital

    Asset haircut table — 16 categories, NOT the 7-category table originally
    specified for this module (cash 0%, government securities 0%, listed
    equities 15%, unlisted equities 25%, property 20%, reinsurance
    recoverables 10%, other assets 25%). Reading the real client workbook
    this was validated against (Worksheets/FCR 2025 Actual - QIC.xlsx,
    "Asset Mix" and "Calculation" sheets) directly, the real asset-discount
    table has 16 categories with DIFFERENT factors — most notably: cash and
    term deposits are 5%, not 0%; property is split into "Investments" (30%)
    vs "Own use" (50%), not one flat 20%; "other assets" is 50%, not 25%.
    The 16-category table below is what's actually implemented — confirmed
    to reproduce this client's real 2025 legacy CAR (117.37%, i.e. "117%")
    EXACTLY, including an exact match on the GHS 16,600,880.15 total asset
    discount (verified cell-by-cell against the real workbook before this
    was written, not guessed). The originally-specified 7-category table
    would not have hit that target.
================================================================================
"""

from dataclasses import dataclass
from typing import Dict

from engine.rbc.data_model import LegacySolvencyInputs

MINIMUM_SOLVENCY_CAPITAL = 10_000_000.0   # GHS floor
NWP_FACTOR = 0.25
MANAGEMENT_EXPENSE_FACTOR = 0.25

# Real 16-category asset-discount table (see module docstring for why this
# supersedes the originally-specified 7-category version).
ASSET_HAIRCUT_FACTORS: Dict[str, float] = {
    "gog_securities":                    0.00,
    "bog_securities":                       0.00,
    "statutory_deposit":                       0.00,
    "cash_and_term_deposits":                     0.05,
    "corporate_debt":                                0.05,
    "listed_equities_gse":                              0.15,
    "other_securities":                                    0.30,
    "equity_backed_mutual_funds":                             0.10,
    "money_market_mutual_funds":                                 0.05,
    "property_investment":                                          0.30,
    "property_own_use":                                                0.50,
    "plant_equipment_furniture":                                          0.50,
    "motor_vehicles":                                                        0.50,
    "ict":                                                                      0.05,
    "reinsurance_recoverables_under_6mo":                                          0.10,
    "other_assets":                                                                   0.50,
}


@dataclass
class LegacySolvencyResult:
    total_capital_resources:    float   # total_capital_base - capital_deductions, before asset haircuts
    total_asset_discounts:        float
    asset_discounts_by_type:         Dict[str, float]
    available_capital_resources:        float   # total_capital_resources - total_asset_discounts

    nwp_based_requirement:                 float
    management_expense_based_requirement:     float
    required_capital:                            float   # MAX(minimum, nwp_based, mgmt_expense_based)

    legacy_car:                                     float
    surplus_deficit:                                   float   # available_capital_resources - required_capital
    status:                                               str   # "STRONG" | "ADEQUATE" | "BREACH" — same thresholds as engine.rbc.aggregation


def calculate_legacy_solvency(inputs: LegacySolvencyInputs) -> LegacySolvencyResult:
    total_capital_resources = inputs.total_capital_base - inputs.capital_deductions

    asset_discounts_by_type = {
        asset_type: round(ASSET_HAIRCUT_FACTORS.get(asset_type, 0.0) * balance, 2)
        for asset_type, balance in inputs.asset_balances.items()
    }
    total_asset_discounts = round(sum(asset_discounts_by_type.values()), 2)

    available_capital_resources = total_capital_resources - total_asset_discounts

    nwp_based = NWP_FACTOR * inputs.net_written_premium
    mgmt_based = MANAGEMENT_EXPENSE_FACTOR * inputs.management_expenses
    required_capital = max(MINIMUM_SOLVENCY_CAPITAL, nwp_based, mgmt_based)

    legacy_car = (available_capital_resources / required_capital) if required_capital > 0 else 0.0
    surplus_deficit = available_capital_resources - required_capital

    if legacy_car >= 1.50:
        status = "STRONG"
    elif legacy_car >= 1.00:
        status = "ADEQUATE"
    else:
        status = "BREACH"

    return LegacySolvencyResult(
        total_capital_resources=round(total_capital_resources, 2),
        total_asset_discounts=total_asset_discounts,
        asset_discounts_by_type=asset_discounts_by_type,
        available_capital_resources=round(available_capital_resources, 2),
        nwp_based_requirement=round(nwp_based, 2),
        management_expense_based_requirement=round(mgmt_based, 2),
        required_capital=round(required_capital, 2),
        legacy_car=round(legacy_car, 6),
        surplus_deficit=round(surplus_deficit, 2),
        status=status,
    )
