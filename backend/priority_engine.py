"""
FIRA – Priority Engine.
Ranks flood zones and assigns evacuation / response priorities based on
pre-computed risk scores and additional triage factors (e.g. population
density, shelter proximity).

Priority tiers:
  1 – Critical  (immediate action required)
  2 – High      (action required within 1 hour)
  3 – Moderate  (monitor closely)
  4 – Low       (routine monitoring)
"""

from typing import List


def rank_zones(zones: List[dict]) -> List[dict]:
    """
    Accepts a list of zone dicts (each containing at minimum a ``risk_score``
    field) and returns the same list sorted by descending risk, with a
    ``priority_rank`` integer added to each entry.

    Parameters
    ----------
    zones : List[dict]
        Zone records, each expected to have at least: id, name, risk_score.

    Returns
    -------
    List[dict]
        Zones sorted by descending risk_score with priority_rank assigned.
    """
    # TODO: implement ranking logic with tiebreaker rules
    raise NotImplementedError("priority_engine.rank_zones is not yet implemented.")
