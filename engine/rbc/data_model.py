"""
================================================================================
RBC DATA MODEL — pure data structures for the GIRBC solvency calculation
================================================================================
What this file does:
    Five dataclasses carrying the exposure/capital inputs the GIRBC risk
    modules (engine/rbc/insurance_risk.py etc.) need. No calculation logic
    lives here — these are the inputs a client's real SDR-shaped workbook
    (engine/data_loader.py's load_rbc_solvency_data()) is parsed into.

Class taxonomy note — read before touching InsuranceRiskExposures or
insurance_risk.py's segment mapping:
    The 11 classes below are Ghana's standard non-life classes. They map to
    5 rating segments for premium/reserve risk-factor purposes. One naming
    collision to be aware of: the raw class "Miscellaneous" (a genuine
    catch-all/other class) is DIFFERENT from the "Miscellaneous" SEGMENT
    (which groups Accident + Bond + Travel + Weather). The raw
    "Miscellaneous" class maps to the "Credit" segment (50%/40% — the
    highest, most conservative factor, appropriate for an unclassified
    catch-all), not to the "Miscellaneous" segment. This was a genuine
    ambiguity in the build spec (which reuses "Miscellaneous" for two
    different things) resolved this way so every one of the 11 classes maps
    to exactly one of the 5 segments with none left over — flagged for
    confirmation, see insurance_risk.py's CLASS_TO_SEGMENT.
================================================================================
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

NON_LIFE_CLASSES: List[str] = [
    "Motor", "Fire", "Accident", "Marine", "Engineering", "Liability",
    "Bond", "Aviation", "Travel", "Weather", "Miscellaneous",
]


# ── Qualifying Capital Resources (Available Capital) ───────────────────────

@dataclass
class QualifyingCapitalResources:
    """
    GIRBC2 — Qualifying Capital Resources (the "Available Capital" side of
    the CAR ratio). Tier 1 Unlimited (ordinary shares + retained earnings +
    contingency reserves, net of deductions) is the base; Tier 1 Limited and
    Tier 2 are capped as a share of the total and excess is either rolled
    down a tier or excluded entirely — see composition_valid()'s docstring
    for exactly how this implementation resolves the circularity in "capped
    at 25% of QCR" when QCR itself depends on the capped amounts.
    """
    tier1_unlimited:             float = 0.0   # ordinary shares + retained earnings + contingency reserves, GROSS (before deductions)
    tier1_unlimited_deductions:  float = 0.0   # goodwill + intangibles + DTAs (deducted from tier1_unlimited)
    tier1_limited:                float = 0.0   # perpetual preference-type instruments, gross (before the 25%-of-QCR cap)
    tier2:                          float = 0.0   # subordinated debt with >5yr remaining term, gross (before the 25%-of-QCR cap)
    tier2_amortisation:              float = 0.0   # reduction applied in the final 5 years before maturity (Table 4 schedule) — subtracted from tier2

    @property
    def net_tier1_unlimited(self) -> float:
        return self.tier1_unlimited - self.tier1_unlimited_deductions

    @property
    def _tier2_after_amortisation(self) -> float:
        return max(0.0, self.tier2 - self.tier2_amortisation)

    @property
    def eligible_tier1_limited(self) -> float:
        """
        Tier 1 Limited is capped at 25% of total QCR — but QCR is itself
        partly made of eligible_tier1_limited, a circular definition. This
        implementation resolves it with a two-pass waterfall (matching the
        directive's stated rule that excess Tier 1 Limited rolls DOWN into
        the Tier 2 pool, rather than a simultaneous-equation solve):
            pass 1: provisional QCR assuming nothing is capped yet
            pass 2: cap Tier 1 Limited at 25% of that provisional QCR
        This is exact whenever nothing actually binds (confirmed against
        QIC's real 2025 filing, which has zero Tier 1 Limited and zero
        Tier 2 — the capping logic is therefore UNVALIDATED against any
        real case where it actually binds; treat it as a reasonable,
        disclosed approximation until a real client exercises this path).
        """
        provisional_qcr = self.net_tier1_unlimited + self.tier1_limited + self._tier2_after_amortisation
        if provisional_qcr <= 0:
            return 0.0
        return min(self.tier1_limited, 0.25 * provisional_qcr)

    @property
    def _tier1_limited_excess(self) -> float:
        """Tier 1 Limited above the 25% cap rolls down into the Tier 2 pool."""
        return max(0.0, self.tier1_limited - self.eligible_tier1_limited)

    @property
    def eligible_tier2(self) -> float:
        """
        Tier 2 (including excess Tier 1 Limited rolled down) capped at 25%
        of QCR, same two-pass waterfall approach as eligible_tier1_limited
        — see that property's docstring. Excess Tier 2 is excluded from QCR
        entirely (not rolled anywhere further).
        """
        tier2_pool = self._tier2_after_amortisation + self._tier1_limited_excess
        provisional_qcr = self.net_tier1_unlimited + self.eligible_tier1_limited + tier2_pool
        if provisional_qcr <= 0:
            return 0.0
        return min(tier2_pool, 0.25 * provisional_qcr)

    @property
    def total_qcr(self) -> float:
        return self.net_tier1_unlimited + self.eligible_tier1_limited + self.eligible_tier2

    @property
    def composition_valid(self) -> bool:
        """True if Tier 1 Unlimited is at least 50% of total QCR, per the directive's composition limit."""
        if self.total_qcr <= 0:
            return self.net_tier1_unlimited >= 0
        return self.net_tier1_unlimited >= 0.5 * self.total_qcr

    def validate(self) -> None:
        """Raises ValueError if the Tier 1 Unlimited >= 50% of QCR composition limit is breached."""
        if not self.composition_valid:
            raise ValueError(
                f"Tier 1 Unlimited (GHS {self.net_tier1_unlimited:,.2f}) is below 50% of total "
                f"Qualifying Capital Resources (GHS {self.total_qcr:,.2f}) — composition limit breached."
            )


# ── Insurance risk (Non-Life premium + claims reserve, by class) ───────────

@dataclass
class InsuranceRiskExposures:
    """
    Per-class net premium and net claims reserve — the two exposure bases
    the Non-Life Insurance risk charge (engine/rbc/insurance_risk.py) is
    computed from. Keys should be a subset of NON_LIFE_CLASSES; a class
    with no exposure can simply be omitted (treated as 0.0).
    """
    net_premium:        Dict[str, float] = field(default_factory=dict)   # by class, e.g. {"Motor": 12_000_000.0, ...}
    # Populated from engine.runner.run_nic_summary()'s net IBNR+OCR output
    # where available (see engine/data_loader.py's load_rbc_solvency_data());
    # a class present in net_premium but absent here is treated as 0.0
    # reserve, not an error — not every class necessarily carries an open
    # claims reserve at a given valuation date.
    net_claims_reserve: Dict[str, float] = field(default_factory=dict)   # by class


# ── Market risk exposures ───────────────────────────────────────────────────

@dataclass
class MarketRiskExposures:
    """
    Balance-sheet exposures the Market Risk charge (engine/rbc/market_risk.py)
    is computed from.
    """
    # By credit rating band (RC1..RC7/Unrated/Default — same bands as
    # CreditRiskExposures.counterparty_exposures). Reserved for a future
    # duration-based Interest Rate module refinement — the current ±250bps
    # net-position formula (see market_risk.py) uses
    # interest_rate_sensitive_assets/liabilities directly, not this
    # breakdown; kept here since it's part of the real SDR7 data a client
    # workbook actually provides.
    bonds_and_fixed_income: Dict[str, float] = field(default_factory=dict)
    # Equity category keys — the real GIRBC4 template's 7 categories (see
    # engine/rbc/market_risk.py's EQUITY_FACTORS): "domestic",
    # "foreign_developed", "foreign_emerging", "unlisted", "hybrid_debt",
    # "related_party_regulated", "related_party_unregulated"
    listed_equities:         Dict[str, float] = field(default_factory=dict)
    # Real estate category keys: "domestic", "foreign"
    real_estate:                Dict[str, float] = field(default_factory=dict)
    # Right-of-use category keys: "owner_occupied", "other_assets", "investment_property"
    right_of_use_assets:          Dict[str, float] = field(default_factory=dict)
    # Net open position by currency code, e.g. {"USD": 250_000.0, "GBP": -40_000.0}
    fx_net_open_position:           Dict[str, float] = field(default_factory=dict)
    interest_rate_sensitive_assets:      float = 0.0
    interest_rate_sensitive_liabilities: float = 0.0


# ── Credit risk exposures ───────────────────────────────────────────────────

@dataclass
class CreditRiskExposures:
    """
    Counterparty/mortgage/other exposures the Credit Risk charge
    (engine/rbc/credit_risk.py) is computed from.
    """
    # (amount, rating_class) pairs — rating_class one of "RC1".."RC7", "Unrated", "Default"
    counterparty_exposures: List[Tuple[float, str]] = field(default_factory=list)
    # (amount, LTV_band) pairs — LTV_band one of "<50%", "50-60%", "60-70%", "70-80%", "80-90%", ">90%"
    mortgage_exposures:      List[Tuple[float, str]] = field(default_factory=list)
    cash_and_deposits:          float = 0.0
    premium_receivables:         float = 0.0
    # UNRATED reinsurance recoverables only (20% factor) — rated RI
    # recoverables go into counterparty_exposures above instead (they use
    # the identical RC1-RC7 table as any other rated counterparty; there's
    # no need for a separate rated-RI field/factor table).
    reinsurance_recoverables:      float = 0.0
    # Receivables from mandatory insurance pools (backed by a governmental
    # entity or jointly by the insurance market) — a distinct 0.7% factor,
    # much lower than ordinary unrated reinsurance recoverables at 20%.
    mandatory_pool_recoverables:      float = 0.0
    deferred_tax_assets:                 float = 0.0
    related_party_loans:                 float = 0.0
    other_receivables:                      float = 0.0


# ── Operational risk exposures ──────────────────────────────────────────────

@dataclass
class OperationalRiskExposures:
    """
    Premium/liability/growth figures the Operational Risk charge
    (engine/rbc/operational_risk.py) is computed from.
    """
    current_year_net_premium:      float = 0.0
    prior_year_net_premium:         float = 0.0
    current_year_net_liabilities:      float = 0.0
    prior_year_net_liabilities:         float = 0.0


# ── Legacy solvency margin inputs (pre-GIRBC transition basis) ─────────────

@dataclass
class LegacySolvencyInputs:
    """
    Inputs for engine/rbc/legacy_solvency.py's older, simpler NIC solvency-
    margin test — what Ghanaian non-life insurers still report as their
    headline CAR during the transition to GIRBC (mandatory 1 Jan 2027).

    Capital resources are modelled as two aggregate figures (gross capital
    base and its non-admissible deductions) rather than every individual
    line item a real filing's capital-resources statement itemises (paid-up
    shares, contingency reserves, retained earnings, intangibles, DTAs,
    connected-person investments, etc — see legacy_solvency.py's module
    docstring for the real client's exact 21-line breakdown this
    aggregates) — same level of simplification as QualifyingCapitalResources
    takes on the GIRBC side.

    asset_balances is keyed by engine.rbc.legacy_solvency.ASSET_HAIRCUT_FACTORS'
    16 real asset-type categories (NOT the 7-category simplification
    originally specified for this module — the real client workbook's own
    asset-discount table has 16 categories with different, more granular
    factors; see that module's docstring for the full disclosed comparison
    and why the 16-category real table was used instead).
    """
    total_capital_base:    float = 0.0   # gross Core + Non-Core capital, before non-admissible deductions
    capital_deductions:      float = 0.0   # non-admissible capital-side deductions (intangibles, DTAs, connected-person investments, encumbered assets, RI receivables >6mo, etc.)
    asset_balances:              Dict[str, float] = field(default_factory=dict)   # by ASSET_HAIRCUT_FACTORS key
    net_written_premium:            float = 0.0
    management_expenses:               float = 0.0
