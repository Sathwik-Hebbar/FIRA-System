"""
FIRA – Priority Engine.
Ranks flood zones and assigns evacuation / response priorities based on
pre-computed risk scores and additional triage factors (e.g. population
density, shelter proximity).

Priority tiers:
  1 – Critical  (immediate action required)   priority_score >= 0.75
  2 – High      (action required within 1 hour) priority_score >= 0.50
  3 – Moderate  (monitor closely)             priority_score >= 0.25
  4 – Low       (routine monitoring)          priority_score < 0.25
"""

from typing import List


def priority_level(score: float) -> str:
    """Convert a numeric priority score (0-1) to a human-readable level."""
    if score >= 0.75:
        return "Critical"
    if score >= 0.50:
        return "High"
    if score >= 0.25:
        return "Moderate"
    return "Low"


def rank_zones(zones: List[dict]) -> List[dict]:
    """
    Accepts a list of zone/report dicts (each containing at minimum a ``risk_score``
    field) and returns the same list sorted by descending risk, with a
    ``priority_rank`` integer added to each entry.

    Parameters
    ----------
    zones : List[dict]
        Zone/report records, each expected to have at least: id, risk_score.

    Returns
    -------
    List[dict]
        Items sorted by descending risk_score with priority_rank assigned (1 = highest).
    """
    sorted_zones = sorted(zones, key=lambda z: z.get("risk_score", 0), reverse=True)
    for idx, z in enumerate(sorted_zones, start=1):
        z["priority_rank"] = idx
    return sorted_zones


def compute_report_priority(report, risk_score: float) -> float:
    """
    Compute a priority score (0.0 – 1.0) for an individual report.

    Combines the zone's environmental risk score with triage factors:
    - Medical emergency   → +0.20 bonus
    - People trapped      → +0.15 bonus (scaled by count, capped)
    - High severity       → weighted by severity level (1-5 → 0-0.10)
    - People affected     → small bonus capped at 0.10

    The result is clamped to [0.0, 1.0].
    """
    score = risk_score  # base: environmental risk (0–1)

    # Medical emergency is a hard boost
    if getattr(report, "medical_emergency", False):
        score += 0.20

    # People trapped – each 10 trapped adds 0.03, capped at 0.15
    trapped = getattr(report, "people_trapped", 0) or 0
    score += min(0.15, (trapped / 10) * 0.03)

    # Severity (1-5) contributes up to 0.10
    sev = getattr(report, "severity", 1) or 1
    score += (sev - 1) / 4 * 0.10

    # People affected – logarithmic contribution capped at 0.10
    affected = getattr(report, "people_affected", 0) or 0
    if affected > 0:
        import math
        score += min(0.10, math.log10(affected + 1) * 0.04)

    return max(0.0, min(1.0, score))
