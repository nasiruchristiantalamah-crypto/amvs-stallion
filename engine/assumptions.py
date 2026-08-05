"""
================================================================================
ASSUMPTIONS MODULE
================================================================================
What this file does:
    Defines ProductAssumptions — every PRICING/VALUATION assumption needed
    to run a projection: mortality loading, lapse rates, expense rates,
    commission structure, discount rate, expense inflation, collection
    rate, risk adjustment method and rate, profit margin target.

    This is deliberately separate from engine/product.py's Product — a
    Product is the STRUCTURE of what's sold (riders, dependants, term);
    ProductAssumptions is the BASIS used to price or value it. The same
    product can be run against different assumption sets (e.g. comparing
    old vs new mortality loadings), and the same assumptions can be
    reused across products with the same market/currency.

    Assumptions are loaded per client, per product, per version via
    engine/assumptions_store.py — nothing here is hardcoded to one
    company or one product anymore.
================================================================================
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Union
from enum import Enum

from data.table_validation import validate_rate_table


# ── Enumerations (fixed choice options) ──────────────────────────────────────

class MeasurementModel(Enum):
    """
    IFRS 17 measurement model.
    GMM = General Measurement Model (long-duration contracts)
    PAA = Premium Allocation Approach (short-duration, <= 12 months)
    VFA = Variable Fee Approach (participating contracts) — future extension
    """
    GMM = "gmm"
    PAA = "paa"
    VFA = "vfa"


class Gender(Enum):
    MALE    = "male"
    FEMALE  = "female"
    UNISEX  = "unisex"


class ReportingFrequency(Enum):
    """How often the client needs IFRS 17 numbers produced."""
    ANNUAL    = "annual"
    QUARTERLY = "quarterly"
    MONTHLY   = "monthly"


# ── Lapse rate schedule ───────────────────────────────────────────────────────

@dataclass
class LapseSchedule:
    """
    Annual lapse rates by policy year — fully client/product-editable, not
    a fixed shape. `rates` takes ANY set of policy years with ANY rates;
    there is no built-in default table, and no assumed number of years —
    a schedule can have one entry or fifty, at whatever policy years the
    client's actual experience data supports (e.g. GLICO Insurance's real
    2025 lapse study covers policy years 1-10, per-product, with rates
    dropping to 0% from year 6 for some products and staying nonzero to
    year 10 for others — there's no universal shape to hardcode).

    How to read:
        { 1: 0.25 } means 25% of in-force policies lapse in policy year 1.
        The highest year present is the "ultimate" rate — applied from
        that year onwards.

    Call validate_lapse_schedule(schedule) before using one built from
    external/user-supplied data — see the validation section below.
    """
    rates: Dict[int, float]

    def get_annual_rate(self, policy_year: int) -> float:
        """
        Step function held at the last defined value: uses the rate at the
        largest defined policy year <= the requested one (so gaps between
        defined years correctly hold the prior rate rather than jumping
        straight to the ultimate rate — needed for sparse, real-world
        schedules like {1: 0.25, 5: 0.09, 13: 0.02} where years 2-4 and
        6-12 aren't individually specified). Requests before the earliest
        defined year use that earliest year's rate.
        """
        years = sorted(self.rates.keys())
        applicable_year = years[0]
        for y in years:
            if y <= policy_year:
                applicable_year = y
            else:
                break
        return self.rates[applicable_year]

    def get_monthly_rate(self, policy_year: int) -> float:
        """monthly = 1 - (1 - annual)^(1/12)"""
        annual = self.get_annual_rate(policy_year)
        return 1.0 - (1.0 - annual) ** (1.0 / 12.0)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LapseSchedule":
        rates = {int(k): float(v) for k, v in d.get("rates", d).items()}
        schedule = cls(rates=rates)
        validate_lapse_schedule(schedule)
        return schedule

    def to_dict(self) -> Dict[str, Any]:
        return {"rates": {str(k): v for k, v in self.rates.items()}}


def validate_lapse_schedule(schedule: "LapseSchedule") -> None:
    """
    Check a lapse schedule for reasonableness: no negative rates, none
    above 100%, and at least one policy year defined. Unlike a mortality
    table, gaps between defined policy years are NOT an error — that's the
    deliberate step-function design (see LapseSchedule.get_annual_rate).

    Called automatically by LapseSchedule.from_dict() — i.e. every time a
    schedule is loaded from a client/product's saved assumptions — so a
    bad table is rejected at load time with a clear reason, not left to
    surface as a silently wrong premium later.

    Raises:
        ValueError, listing every problem found, if the schedule isn't valid.
    """
    validate_rate_table(
        schedule.rates, label="lapse schedule", key_label="policy year", require_contiguous=False,
    )


# ── Commission schedule ───────────────────────────────────────────────────────

@dataclass
class CommissionSchedule:
    """
    Commission rates paid to agents/brokers. Two ways to configure:

      1. Simple two-tier: initial_rate (policy year 1) + renewal_rate
         (policy year 2 onwards) — the common structure, and still the
         default if nothing else is given.
      2. Full per-policy-year schedule via `rates` — e.g. GLICO's real
         commission structure pays a different rate in each of policy
         years 1, 2, 3, 4, and 5+:
             rates = {1: 0.30, 2: 0.15, 3: 0.10, 4: 0.05, 5: 0.02}
         When `rates` is given (non-empty) it takes priority over
         initial_rate/renewal_rate entirely. Lookup is a step function held
         at the last defined year — same design as LapseSchedule's
         get_annual_rate — so a sparse schedule like {1: 0.30, 5: 0.02}
         holds 30% for years 2-4 too, and 2% from year 5 onwards.

    Validated at construction (via from_dict, or directly by calling
    validate_commission_schedule()): every rate must be between 0% and
    100% — a commission rate can't be negative or exceed the premium
    itself.
    """
    initial_rate:  float                = 0.15    # Policy year 1 (used only if `rates` is not set)
    renewal_rate:  float                = 0.02    # Policy year 2+ (used only if `rates` is not set)
    rates:         Optional[Dict[int, float]] = None    # {policy_year: rate} — overrides initial/renewal when set

    def get_rate_for_policy_year(self, policy_year: int) -> float:
        if self.rates:
            years = sorted(self.rates.keys())
            applicable_year = years[0]
            for y in years:
                if y <= policy_year:
                    applicable_year = y
                else:
                    break
            return self.rates[applicable_year]
        return self.initial_rate if policy_year <= 1 else self.renewal_rate

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CommissionSchedule":
        kwargs = dict(d)
        kwargs.pop("type", None)   # discriminator used by ProductAssumptions.from_dict, not a real field
        if kwargs.get("rates"):
            kwargs["rates"] = {int(k): float(v) for k, v in kwargs["rates"].items()}
        schedule = cls(**kwargs)
        validate_commission_schedule(schedule)
        return schedule

    def to_dict(self) -> Dict[str, Any]:
        # "type" lets ProductAssumptions.from_dict tell a percentage
        # schedule apart from a FixedScaleCommission when deserializing —
        # both dataclasses share no distinguishing field shape otherwise.
        d = dict(type="percentage", initial_rate=self.initial_rate, renewal_rate=self.renewal_rate)
        if self.rates:
            d["rates"] = {str(k): v for k, v in self.rates.items()}
        return d


def validate_commission_schedule(schedule: "CommissionSchedule") -> None:
    """
    Check a commission schedule for reasonableness: every rate between 0%
    and 100%. Validates whichever form is actually in use — the full
    per-policy-year `rates` table if set, otherwise the simple
    initial_rate/renewal_rate pair.

    Called automatically by CommissionSchedule.from_dict() — i.e. every
    time a schedule is loaded from a client/product's saved assumptions —
    so a bad table is rejected at load time, not left to silently produce
    a wrong commission expense later.

    Raises:
        ValueError, listing every problem found, if the schedule isn't valid.
    """
    if schedule.rates:
        validate_rate_table(
            schedule.rates, label="commission schedule", key_label="policy year", require_contiguous=False,
        )
    else:
        validate_rate_table(
            {1: schedule.initial_rate, 2: schedule.renewal_rate},
            label="commission schedule", key_label="policy year", require_contiguous=False,
        )


# ── Fixed-scale commission (flat GHS per policy, not a % of premium) ──────────

@dataclass
class FixedScaleCommission:
    """
    An alternative to CommissionSchedule for distribution structures that
    pay agents a flat GHS amount per in-force policy per year — not a
    percentage of premium — varying by policy year. Common in Ghanaian
    microinsurance as a "transport support" allowance layered on top of
    (or instead of) a percentage commission.

    rates = {policy_year: annual GHS amount per policy}, e.g. the named
    "Transport Commission Scale (GHS 700 Base)" preset:
        {1: 910.0, 2: 840.0, 3: 700.0, 4: 560.0, 5: 490.0}
    — a scaled allowance off a GHS 700 base rate (130% in year 1, 120% in
    year 2, 100% in year 3, 80% in year 4, 70% from year 5 onward).

    Lookup is the same step-function-held-at-last-defined-year design as
    CommissionSchedule.rates and LapseSchedule — a sparse schedule like
    {1: 910.0, 5: 490.0} holds GHS 910 for years 2-4 too.

    engine/cashflows.py checks which type ProductAssumptions.commission
    actually is (isinstance) and applies GHS per in-force policy here
    instead of a percentage of that month's premium.
    """
    rates: Dict[int, float]   # {policy_year: annual GHS amount per policy}

    def get_annual_amount_for_policy_year(self, policy_year: int) -> float:
        years = sorted(self.rates.keys())
        applicable_year = years[0]
        for y in years:
            if y <= policy_year:
                applicable_year = y
            else:
                break
        return self.rates[applicable_year]

    def get_monthly_amount_for_policy_year(self, policy_year: int) -> float:
        return self.get_annual_amount_for_policy_year(policy_year) / 12.0

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FixedScaleCommission":
        rates = {int(k): float(v) for k, v in d.get("rates", d).items()}
        schedule = cls(rates=rates)
        validate_fixed_scale_commission(schedule)
        return schedule

    def to_dict(self) -> Dict[str, Any]:
        # "type" lets ProductAssumptions.from_dict tell this apart from a
        # percentage CommissionSchedule when deserializing.
        return {"type": "fixed_scale", "rates": {str(k): v for k, v in self.rates.items()}}


def validate_fixed_scale_commission(schedule: "FixedScaleCommission") -> None:
    """
    Check a fixed-scale commission schedule for reasonableness: no
    negative GHS amounts, and at least one policy year defined. Unlike a
    percentage schedule there's no upper bound to check (a GHS amount
    isn't capped at 100 the way a rate is).
    """
    if not schedule.rates:
        raise ValueError("fixed-scale commission schedule must have at least one policy year defined")
    negative = {y: v for y, v in schedule.rates.items() if v < 0}
    if negative:
        raise ValueError(f"fixed-scale commission schedule has negative GHS amounts: {negative}")


def transport_commission_scale_ghs700_base() -> "FixedScaleCommission":
    """
    Named preset: "Transport Commission Scale (GHS 700 Base)" — a real
    Ghanaian microinsurance distribution structure, scaled off a GHS 700
    base rate (130% / 120% / 100% / 80% / 70% for policy years 1-5+).
    """
    return FixedScaleCommission(rates={1: 910.0, 2: 840.0, 3: 700.0, 4: 560.0, 5: 490.0})


# ── Main assumptions dataclass ────────────────────────────────────────────────

@dataclass
class ProductAssumptions:
    """
    Complete set of pricing/valuation assumptions for one run. Everything
    here is a true *assumption* (a basis choice) rather than a *product
    structure* choice (which lives in engine/product.py's Product).
    """

    # ── Run identification ─────────────────────────────────────────────────
    company_name:       str            = "Insurer Name"
    run_date:           str            = "2026-01-01"  # YYYY-MM-DD
    reporting_currency: str            = "GHS"
    fx_rate_ghs_usd:    float          = 15.50

    # ── IFRS 17 settings ───────────────────────────────────────────────────
    reporting_frequency: ReportingFrequency = ReportingFrequency.ANNUAL
    cohort_year:          int                = 2026

    # ── Policyholder demographics ──────────────────────────────────────────
    entry_age_main: int    = 35
    gender_main:    Gender = Gender.UNISEX
    gender_main_str: str   = "unisex"

    # ── Policy structure (terminal age for the projection horizon) ─────────
    max_age: int = 80   # Mortality table terminal age / whole-life projection end

    # ── Mortality assumptions ──────────────────────────────────────────────
    mortality_loading: float = -0.20   # -20% = 20% better than table

    # ── Lapse assumptions ──────────────────────────────────────────────────
    # Required, no default — every client/product must supply its own real
    # lapse experience (or a deliberately chosen placeholder); the engine
    # will not silently fall back to a fabricated schedule. kw_only so this
    # can be a required field without reordering the many defaulted fields
    # around it (every call site in this codebase already uses keyword args).
    lapse_schedule: LapseSchedule = field(kw_only=True)

    # ── Premium collection ─────────────────────────────────────────────────
    collection_rate: float = 0.70

    # ── Economic assumptions ───────────────────────────────────────────────
    valuation_rate_pa:    float = 0.165   # IFRS 17 discount rate
    investment_rate_pa:   float = 0.140
    expense_inflation_pa: float = 0.120
    locked_in_rate_pa:    float = 0.165   # Set at inception, never changes (CSM accretion)
    usd_risk_free_rate:   float = 0.045

    @property
    def valuation_rate_monthly(self) -> float:
        return (1 + self.valuation_rate_pa) ** (1 / 12) - 1

    @property
    def investment_rate_monthly(self) -> float:
        return (1 + self.investment_rate_pa) ** (1 / 12) - 1

    @property
    def expense_inflation_monthly(self) -> float:
        return (1 + self.expense_inflation_pa) ** (1 / 12) - 1

    @property
    def locked_in_rate_monthly(self) -> float:
        return (1 + self.locked_in_rate_pa) ** (1 / 12) - 1

    # ── Expense assumptions ────────────────────────────────────────────────
    policy_fee_monthly:     float = 1.00
    acquisition_cost:       float = 8.00
    renewal_expense_annual: float = 18.00
    claims_admin_cost:      float = 5.00

    @property
    def renewal_expense_monthly(self) -> float:
        return self.renewal_expense_annual / 12

    # ── Commission ─────────────────────────────────────────────────────────
    # Either a percentage-of-premium CommissionSchedule (the common case)
    # or a FixedScaleCommission (flat GHS per policy per year, e.g. a
    # "transport support" allowance) — engine/cashflows.py checks which
    # one this actually is at calculation time.
    commission: Union[CommissionSchedule, FixedScaleCommission] = field(default_factory=CommissionSchedule)

    # ── Profit target ──────────────────────────────────────────────────────
    target_profit_margin: float = 0.15

    # ── Risk adjustment (IFRS 17) ──────────────────────────────────────────
    ra_method:            str   = "cost_of_capital"   # or "confidence_level" / "percentile"
    coc_rate:              float = 0.06
    solvency_capital:      float = 500_000
    ra_confidence_level:   float = 0.75

    @property
    def risk_adjustment(self) -> float:
        """Annual Risk Adjustment using Cost of Capital method. IFRS 17 B91-B92."""
        return self.coc_rate * self.solvency_capital

    # ── Serialization ──────────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ProductAssumptions":
        d = dict(d)
        if "reporting_frequency" in d and isinstance(d["reporting_frequency"], str):
            d["reporting_frequency"] = ReportingFrequency(d["reporting_frequency"])
        if "gender_main" in d and isinstance(d["gender_main"], str):
            d["gender_main"] = Gender(d["gender_main"])
        if "lapse_schedule" not in d:
            raise ValueError(
                "ProductAssumptions requires a 'lapse_schedule' — there is no built-in "
                "default. Supply {'rates': {policy_year: annual_rate, ...}} with at "
                "least one policy year."
            )
        d["lapse_schedule"] = LapseSchedule.from_dict(d["lapse_schedule"])
        if "commission" in d:
            if d["commission"].get("type") == "fixed_scale":
                d["commission"] = FixedScaleCommission.from_dict(d["commission"])
            else:
                d["commission"] = CommissionSchedule.from_dict(d["commission"])
        return cls(**d)

    def to_dict(self) -> Dict[str, Any]:
        return dict(
            company_name=self.company_name, run_date=self.run_date,
            reporting_currency=self.reporting_currency, fx_rate_ghs_usd=self.fx_rate_ghs_usd,
            reporting_frequency=self.reporting_frequency.value, cohort_year=self.cohort_year,
            entry_age_main=self.entry_age_main, gender_main=self.gender_main.value,
            gender_main_str=self.gender_main_str, max_age=self.max_age,
            mortality_loading=self.mortality_loading,
            lapse_schedule=self.lapse_schedule.to_dict(),
            collection_rate=self.collection_rate,
            valuation_rate_pa=self.valuation_rate_pa, investment_rate_pa=self.investment_rate_pa,
            expense_inflation_pa=self.expense_inflation_pa, locked_in_rate_pa=self.locked_in_rate_pa,
            usd_risk_free_rate=self.usd_risk_free_rate,
            policy_fee_monthly=self.policy_fee_monthly, acquisition_cost=self.acquisition_cost,
            renewal_expense_annual=self.renewal_expense_annual, claims_admin_cost=self.claims_admin_cost,
            commission=self.commission.to_dict(), target_profit_margin=self.target_profit_margin,
            ra_method=self.ra_method, coc_rate=self.coc_rate, solvency_capital=self.solvency_capital,
            ra_confidence_level=self.ra_confidence_level,
        )
