"""
FIRA – Risk Engine.
Computes a normalised risk score (0.0 – 1.0) for a flood zone based on
sensor readings and geographic/historical factors.

Score interpretation:
  0.00 – 0.39  → Low risk
  0.40 – 0.69  → Moderate risk
  0.70 – 1.00  → High risk
"""


def compute_risk_score(
    rainfall_mm: float,
    river_level_m: float,
    danger_level_m: float,
    elevation_m: float,
    drainage_quality: float,
    historical_frequency: float,
) -> float:
    """
    Returns a risk score between 0.0 and 1.0.

    Parameters
    ----------
    rainfall_mm : float
        Current rainfall in millimetres.
    river_level_m : float
        Current river / water-body level in metres.
    danger_level_m : float
        Official danger threshold level in metres for this zone.
    elevation_m : float
        Zone elevation above sea level in metres.
    drainage_quality : float
        Drainage infrastructure quality, 0.0 (very poor) to 1.0 (excellent).
    historical_frequency : float
        Historical flood frequency for this zone, 0.0 (never) to 1.0 (very frequent).

    Returns
    -------
    float
        Normalised risk score clamped to [0.0, 1.0].
    """
    # TODO: implement weighted scoring model
    raise NotImplementedError("risk_engine.compute_risk_score is not yet implemented.")
