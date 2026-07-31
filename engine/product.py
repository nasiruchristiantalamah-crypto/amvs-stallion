"""
================================================================================
PRODUCT MODEL
================================================================================
What this file does:
    Defines a generic, client-configurable product structure: any number of
    benefit riders (death, TPD, hospitalization, critical illness, funeral,
    education, income protection, or anything else a client sells), any
    number of dependants, and a reinsurance structure per rider.

    This replaces the old hardcoded BenefitPackage (fixed to exactly
    death/TPD/hospitalization/funeral) and the single optional dependant
    that used to live in engine/assumptions.py. Nothing about a product's
    shape is hardcoded in Python — a new rider type or dependant
    relationship needs a new line in a client's YAML file, not a code
    change.

    A Product is loaded from clients/<client_id>/products/<name>.yaml —
    see engine/clients.py for the loader.
================================================================================
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RiderReinsurance:
    """Reinsurance structure for one rider."""
    has_reinsurance: bool  = False
    treaty_type:      str  = "quota_share"   # "quota_share", "surplus", "excess_of_loss"
    retention_rate:   float = 1.0            # Fraction retained by the insurer (1.0 = no cession)
    currency:         str  = "GHS"

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "RiderReinsurance":
        return cls(**d) if d else cls()

    def to_dict(self) -> Dict[str, Any]:
        return dict(has_reinsurance=self.has_reinsurance, treaty_type=self.treaty_type,
                    retention_rate=self.retention_rate, currency=self.currency)


@dataclass
class Rider:
    """
    One benefit rider on a product.

    rider_type is a free-form string, not a closed enum — "death", "tpd",
    "hospitalization", "critical_illness", "funeral", "education",
    "income_protection", or anything a client needs. Adding a new type
    requires no code change.

    incidence_basis:
        "mortality" — this rider pays out on death, driven by the
            decrement table's dx (main life and/or dependants).
        anything else — treated as an annual incidence rate applied to
            in-force lives, exactly like the old hardcoded TPD/hospital
            logic, but now generic to any non-mortality rider.
    """
    rider_type:            str
    name:                   str
    benefit_main:           float = 0.0    # Benefit amount for the main life
    benefit_dependant:      float = 0.0    # Default benefit amount per dependant

    incidence_basis:        str   = "mortality"
    annual_incidence_rate:  float = 0.0    # Used when incidence_basis != "mortality"
    avg_events_per_year:    float = 1.0    # e.g. hospitalization's "average days" multiplier

    waiting_period_months:  int = 0
    max_duration_months:    Optional[int] = None   # None = runs for the full policy term

    reinsurance: RiderReinsurance = field(default_factory=RiderReinsurance)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Rider":
        d = dict(d)
        d["reinsurance"] = RiderReinsurance.from_dict(d.get("reinsurance"))
        return cls(**d)

    def to_dict(self) -> Dict[str, Any]:
        return dict(
            rider_type=self.rider_type, name=self.name,
            benefit_main=self.benefit_main, benefit_dependant=self.benefit_dependant,
            incidence_basis=self.incidence_basis, annual_incidence_rate=self.annual_incidence_rate,
            avg_events_per_year=self.avg_events_per_year,
            waiting_period_months=self.waiting_period_months, max_duration_months=self.max_duration_months,
            reinsurance=self.reinsurance.to_dict(),
        )


@dataclass
class Dependant:
    """One covered dependant life."""
    relationship:       str              # "spouse", "parent", "child", or any client-defined relationship
    age:                Optional[int] = None   # explicit age; if None, age_offset is used
    age_offset:         int = 0          # applied to the main life's entry age when age is None
    benefit_multiplier: float = 1.0      # scales each rider's benefit_dependant for this dependant

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Dependant":
        return cls(**d)

    def to_dict(self) -> Dict[str, Any]:
        return dict(relationship=self.relationship, age=self.age,
                    age_offset=self.age_offset, benefit_multiplier=self.benefit_multiplier)


@dataclass
class Product:
    """
    A complete product definition — the structural shape of what's sold,
    kept separate from ProductAssumptions (which holds the pricing/
    valuation assumptions applied to it).
    """
    name:               str
    policy_term_years:  Optional[int] = None   # None = whole life (runs to assumptions.max_age)
    premium_mode:       str = "monthly"
    measurement_model:  str = "gmm"            # "gmm" or "paa"
    riders:             List[Rider]     = field(default_factory=list)
    dependants:         List[Dependant] = field(default_factory=list)

    def get_dependant_age(self, dependant: Dependant, main_entry_age: int) -> int:
        if dependant.age is not None:
            return dependant.age
        return main_entry_age + dependant.age_offset

    def coverage_end_age(self, main_entry_age: int, max_age: int) -> int:
        """
        The age at which cover ends: entry_age + policy_term_years for a
        term product, or max_age for whole life (policy_term_years is None).
        """
        if self.policy_term_years is None:
            return max_age
        return min(main_entry_age + self.policy_term_years, max_age)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Product":
        d = dict(d)
        d["riders"] = [Rider.from_dict(r) for r in d.get("riders", [])]
        d["dependants"] = [Dependant.from_dict(x) for x in d.get("dependants", [])]
        return cls(**d)

    def to_dict(self) -> Dict[str, Any]:
        return dict(
            name=self.name, policy_term_years=self.policy_term_years,
            premium_mode=self.premium_mode, measurement_model=self.measurement_model,
            riders=[r.to_dict() for r in self.riders],
            dependants=[d.to_dict() for d in self.dependants],
        )
