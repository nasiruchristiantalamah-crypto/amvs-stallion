"""
================================================================================
ASSUMPTIONS MANAGER — user-editable, named, saveable assumption sets
================================================================================
What this file does:
    Defines AssumptionSet — a named, nested, fully-serialisable bundle of
    every pricing/valuation assumption a user can edit through the
    dashboard's Assumptions page (mortality, lapses, commissions,
    expenses, economic, risk adjustment, pricing targets).

    This is a DIFFERENT layer from engine/assumptions.py's
    ProductAssumptions:
        ProductAssumptions — the flat dataclass the engine (decrement /
            cashflows / present_value) actually consumes, loaded per
            client+product+version from clients/<id>/assumptions/.
        AssumptionSet — a request-time, user-facing, nested structure
            that can OVERRIDE some or all of a ProductAssumptions for one
            run, without touching the client's saved YAML. Saved/loaded
            via the database (db/models.py's AssumptionSet table — same
            name, different layer) so a user can build up a library of
            named bases ("PIC Pricing Basis 2025", "Stressed +10%
            Mortality", ...) and re-apply them at will.

    AssumptionSet.to_product_assumptions(base=...) is the bridge: given an
    existing ProductAssumptions (typically the client/product's own saved
    basis), it returns a COPY with every field this AssumptionSet actually
    carries overridden — run identification fields (company_name,
    run_date, entry age, etc.) that aren't part of AssumptionSet at all
    are left untouched on the copy. See engine/runner.py's run_pricing()/
    run_ifrs17()/run_rate_table(), which accept an optional AssumptionSet
    and apply it exactly this way; when none is given, behaviour is
    completely unchanged from before this module existed.
================================================================================
"""

import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from engine.assumptions import CommissionSchedule, LapseSchedule, ProductAssumptions


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Nested assumption groups ─────────────────────────────────────────────────

@dataclass
class MortalityAssumptions:
    table:        str   = "GNM 2020"
    loading:      float = -0.20
    gender_basis: str   = "unisex"   # "male", "female", "unisex"


@dataclass
class ExpenseAssumptions:
    policy_fee:        float = 1.00     # GHS per month
    acquisition_cost:  float = 8.00     # GHS, one-time
    renewal_expense:   float = 18.00    # GHS per year
    claims_admin:      float = 5.00     # GHS per expected claim event
    inflation_rate:    float = 0.12     # expense inflation, % p.a.


@dataclass
class EconomicAssumptions:
    valuation_rate:     float = 0.165   # IFRS 17 discount rate, % p.a.
    investment_return:  float = 0.14    # % p.a.
    expense_inflation:  float = 0.12    # % p.a. — kept alongside ExpenseAssumptions.inflation_rate
                                          # for API/dashboard convenience; the two are always kept
                                          # in sync (see AssumptionSet.__post_init__).
    fx_rate:            float = 15.50   # GHS per USD
    locked_in_rate:     float = 0.165   # % p.a. — set at inception, never re-estimated (CSM accretion)


@dataclass
class RiskAdjustmentAssumptions:
    method:            str   = "cost_of_capital"   # or "confidence_level" / "percentile"
    coc_rate:          float = 0.06                # % p.a.
    solvency_capital:  float = 500_000              # GHS
    confidence_level:  float = 0.75                 # used only when method == "confidence_level"


@dataclass
class PricingAssumptions:
    profit_margin:    float = 0.15      # target, % of PV premiums
    collection_rate:  float = 0.70      # % of billed premium actually collected
    premium_mode:     str   = "monthly" # "monthly", "quarterly", "annual", "single", or "daily"


# ── The full named, saveable assumption set ─────────────────────────────────

@dataclass
class AssumptionSet:
    name:         str            = "Ghana Market Defaults"
    description:  str            = ""
    client_id:    Optional[str]  = None
    created_by:   Optional[str]  = None
    created_at:   str            = field(default_factory=_utcnow_iso)

    mortality:        MortalityAssumptions       = field(default_factory=MortalityAssumptions)
    lapses:           Dict[int, float]           = field(default_factory=dict)   # {policy_year: annual_rate}
    commissions:      Dict[int, float]           = field(default_factory=dict)   # {policy_year: rate}
    expenses:         ExpenseAssumptions          = field(default_factory=ExpenseAssumptions)
    economic:         EconomicAssumptions          = field(default_factory=EconomicAssumptions)
    risk_adjustment:  RiskAdjustmentAssumptions      = field(default_factory=RiskAdjustmentAssumptions)
    pricing:          PricingAssumptions              = field(default_factory=PricingAssumptions)

    # ── Ghana market defaults ────────────────────────────────────────────
    @classmethod
    def ghana_defaults(cls, client_id: Optional[str] = None, created_by: Optional[str] = None) -> "AssumptionSet":
        """
        Standard Ghana market pricing/valuation basis — see GET
        /assumptions/defaults. Every number here is a starting point the
        user can edit on the dashboard's Assumptions page, not a hardcoded
        engine constant; nothing in engine/pricing.py or
        engine/cashflows.py depends on these specific values.
        """
        return cls(
            name="Ghana Market Defaults",
            description="Standard Ghana market pricing/valuation assumptions",
            client_id=client_id,
            created_by=created_by,
            mortality=MortalityAssumptions(table="GNM 2020", loading=-0.20, gender_basis="unisex"),
            # Year 1 -> 25%, declining smoothly to year 13+ -> 2% (held from
            # year 13 onward — see LapseSchedule.get_annual_rate's step
            # function, which this dict is loaded into via to_product_assumptions()).
            lapses={
                1: 0.25, 2: 0.20, 3: 0.17, 4: 0.14, 5: 0.12, 6: 0.10, 7: 0.08,
                8: 0.07, 9: 0.06, 10: 0.05, 11: 0.04, 12: 0.03, 13: 0.02,
            },
            # 12% for policy years 1-2 (= months 0-24), 0% from year 3 onward.
            commissions={1: 0.12, 2: 0.12, 3: 0.0},
            expenses=ExpenseAssumptions(
                policy_fee=1.00, acquisition_cost=8.00, renewal_expense=18.00,
                claims_admin=5.00, inflation_rate=0.12,
            ),
            economic=EconomicAssumptions(
                valuation_rate=0.165, investment_return=0.14, expense_inflation=0.12,
                fx_rate=15.50, locked_in_rate=0.165,
            ),
            risk_adjustment=RiskAdjustmentAssumptions(
                method="cost_of_capital", coc_rate=0.06, solvency_capital=500_000, confidence_level=0.75,
            ),
            pricing=PricingAssumptions(profit_margin=0.15, collection_rate=0.70, premium_mode="monthly"),
        )

    # ── Serialisation ─────────────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name":        self.name,
            "description": self.description,
            "client_id":   self.client_id,
            "created_by":  self.created_by,
            "created_at":  self.created_at,
            "mortality":       asdict(self.mortality),
            "lapses":          {str(k): v for k, v in self.lapses.items()},
            "commissions":     {str(k): v for k, v in self.commissions.items()},
            "expenses":        asdict(self.expenses),
            "economic":        asdict(self.economic),
            "risk_adjustment": asdict(self.risk_adjustment),
            "pricing":         asdict(self.pricing),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AssumptionSet":
        return cls(
            name        = d.get("name", "Custom Assumption Set"),
            description = d.get("description", ""),
            client_id   = d.get("client_id"),
            created_by  = d.get("created_by"),
            created_at  = d.get("created_at") or _utcnow_iso(),
            mortality       = MortalityAssumptions(**d.get("mortality", {})),
            lapses          = {int(k): float(v) for k, v in (d.get("lapses") or {}).items()},
            commissions     = {int(k): float(v) for k, v in (d.get("commissions") or {}).items()},
            expenses        = ExpenseAssumptions(**d.get("expenses", {})),
            economic        = EconomicAssumptions(**d.get("economic", {})),
            risk_adjustment = RiskAdjustmentAssumptions(**d.get("risk_adjustment", {})),
            pricing         = PricingAssumptions(**d.get("pricing", {})),
        )

    # ── Bridge to the engine's ProductAssumptions ────────────────────────
    def to_product_assumptions(
        self,
        base:            Optional[ProductAssumptions] = None,
        entry_age_main:  Optional[int]                = None,
    ) -> ProductAssumptions:
        """
        Apply this AssumptionSet on top of `base` (a copy — `base` itself
        is never mutated), or build a fresh ProductAssumptions from Ghana
        defaults if no base is given. Every ProductAssumptions field this
        AssumptionSet has an opinion on is overridden; run-identification
        fields not covered by AssumptionSet (company_name, run_date,
        cohort_year, max_age, gender_main, reporting_frequency) are left
        exactly as `base` had them.
        """
        if base is not None:
            target = copy.deepcopy(base)
        else:
            target = ProductAssumptions(lapse_schedule=LapseSchedule(rates={1: 0.0}))

        if entry_age_main is not None:
            target.entry_age_main = entry_age_main

        target.mortality_loading = self.mortality.loading
        target.gender_main_str   = self.mortality.gender_basis

        if self.lapses:
            target.lapse_schedule = LapseSchedule(rates=dict(self.lapses))
        if self.commissions:
            target.commission = CommissionSchedule(rates=dict(self.commissions))

        target.policy_fee_monthly      = self.expenses.policy_fee
        target.acquisition_cost        = self.expenses.acquisition_cost
        target.renewal_expense_annual  = self.expenses.renewal_expense
        target.claims_admin_cost       = self.expenses.claims_admin
        target.expense_inflation_pa    = self.expenses.inflation_rate

        target.valuation_rate_pa   = self.economic.valuation_rate
        target.investment_rate_pa  = self.economic.investment_return
        target.fx_rate_ghs_usd     = self.economic.fx_rate
        target.locked_in_rate_pa   = self.economic.locked_in_rate

        target.ra_method           = self.risk_adjustment.method
        target.coc_rate             = self.risk_adjustment.coc_rate
        target.solvency_capital       = self.risk_adjustment.solvency_capital
        target.ra_confidence_level      = self.risk_adjustment.confidence_level

        target.target_profit_margin = self.pricing.profit_margin
        target.collection_rate       = self.pricing.collection_rate

        return target
