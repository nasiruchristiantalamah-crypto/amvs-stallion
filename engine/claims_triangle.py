"""
================================================================================
CLAIMS TRIANGLE MODULE
================================================================================
What this file does:
    Defines the data structure for a cumulative claims development triangle —
    the general insurance (non-life) equivalent of the decrement projection
    used on the life side. This is the raw input to the Chain Ladder engine
    in chain_ladder.py.

    A triangle has one row per origin year (accident/underwriting year) and
    one column per development period (age since the origin year began).
    Each cell is the CUMULATIVE incurred (or paid) amount known as at that
    development age. The bottom-right is empty because those years haven't
    developed that far yet — that's the "IBNR" the Chain Ladder module fills in.

Reference workpaper:
    Provident Insurance (PIC) — "2025 IBNR Projection (Gross & Net) - Final.xlsx"
    Sheet layout per class (MOTOR, FIRE, ACCIDENT, OTHERS): a "Gross Incurred
    Triangle" and "Net Incurred Triangle" side by side, each with an
    incremental view and a "Cummulative Loss Reported" view. This module
    mirrors the cumulative view.
================================================================================
"""

from dataclasses import dataclass
from typing import Dict, List


@dataclass
class ClaimsTriangle:
    """
    A cumulative claims development triangle for one class of business.

    class_of_business : label for reporting, e.g. "Motor"
    origin_years       : row labels (accident/underwriting years), oldest first
    triangle            : origin_year -> list of cumulative incurred values by
                           development age (index 0 = age 0, i.e. as at the
                           end of the origin year itself). Each row is only as
                           long as the number of diagonals actually observed
                           for that origin year — older years have longer rows.
    """
    class_of_business: str
    origin_years:       List[int]
    triangle:            Dict[int, List[float]]

    @property
    def num_periods(self) -> int:
        """Number of development periods spanned by the oldest origin year."""
        return len(self.origin_years)

    def latest_cumulative(self, origin_year: int) -> float:
        """The most recent (rightmost / latest diagonal) observed cumulative value."""
        row = self.triangle[origin_year]
        return row[-1] if row else 0.0

    def latest_dev_age(self, origin_year: int) -> int:
        """0-indexed development age of the latest observed diagonal for this origin year."""
        return len(self.triangle[origin_year]) - 1

    def validate(self) -> None:
        """
        Sanity-check the triangle shape: each origin year's row must not be
        longer than an older origin year's row (can't have observed more
        development than an earlier-starting year has had time to reach).
        """
        for i in range(1, len(self.origin_years)):
            older = self.triangle[self.origin_years[i - 1]]
            newer = self.triangle[self.origin_years[i]]
            if len(newer) > len(older):
                raise ValueError(
                    f"{self.class_of_business}: origin year {self.origin_years[i]} "
                    f"has more development periods ({len(newer)}) than the older "
                    f"origin year {self.origin_years[i - 1]} ({len(older)}) — "
                    f"triangle is not a valid upper-left triangle."
                )
