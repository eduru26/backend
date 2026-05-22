"""Grade numbering.

Primary/secondary grades are 1-12. Pre-primary levels sit *below* grade 1 as
non-positive integers so that the natural `grade + 1` promotion chain still
works: Pre-KG (-2) -> LKG (-1) -> UKG (0) -> Grade 1.
"""
from __future__ import annotations

# grade int -> short label
PRE_PRIMARY: dict[int, str] = {-2: "Pre-KG", -1: "LKG", 0: "UKG"}

# Default grades a brand-new school is provisioned with (pre-primary + 1..10).
DEFAULT_GRADES: list[int] = [-2, -1, 0, *range(1, 11)]


def grade_label(grade: int) -> str:
    """Human label, e.g. -1 -> 'LKG', 5 -> 'Grade 5'."""
    return PRE_PRIMARY.get(grade, f"Grade {grade}")


def classroom_name(grade: int, section: str = "A") -> str:
    """Default classroom name, e.g. (-1,'A') -> 'LKG - Section A'."""
    if grade in PRE_PRIMARY:
        return f"{PRE_PRIMARY[grade]} - Section {section}"
    return f"Grade {grade} - Section {section}"
