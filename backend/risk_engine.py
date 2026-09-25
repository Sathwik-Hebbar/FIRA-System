"""
FIRA – Risk Scoring Engine (Step 4).
Computes a transparent, explainable, rule-based flood-risk score (0.0 – 1.0)
for a flood zone based on environmental readings and geographic factors.

Score Interpretation / Risk Levels:
  0.00 – 0.24  → Low
  0.25 – 0.49  → Moderate
  0.50 – 0.74  → High
  0.75 – 1.00  → Severe
"""

import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Ensure backend directory is in sys.path for direct imports
_backend_dir = str(Path(__file__).resolve().parent)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from sqlalchemy.orm import Session

from models import Zone

# ---------------------------------------------------------------------------
# Formula Weights (must sum to exactly 1.00)
# ---------------------------------------------------------------------------
WEIGHT_RAINFALL = 0.35
WEIGHT_RIVER = 0.30
WEIGHT_HISTORICAL = 0.15
WEIGHT_ELEVATION = 0.10
WEIGHT_DRAINAGE = 0.10


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    """Clamp a numerical value between a minimum and maximum boundary."""
    return max(minimum, min(value, maximum))


def _extract_numeric(zone: Any, field_name: str) -> float:
    """
    Extract a numeric attribute or dictionary key from a zone object,
    raising a clear error if missing, None, or not numeric.
    """
    if hasattr(zone, field_name):
        val = getattr(zone, field_name)
    elif isinstance(zone, dict) and field_name in zone:
        val = zone[field_name]
    else:
        raise AttributeError(f"Zone is missing required field: '{field_name}'")

    if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
        raise TypeError(
            f"Field '{field_name}' must be a numeric value, got: {val!r}"
        )

    return float(val)


# ---------------------------------------------------------------------------
# Main Risk Computation Function
# ---------------------------------------------------------------------------

def compute_risk(zone: Any) -> Dict[str, Any]:
    """
    Computes an explainable rule-based flood risk assessment for a Zone.

    Parameters
    ----------
    zone : Zone or dict
        A SQLAlchemy Zone instance or a dictionary containing:
        rainfall_mm, river_level_m, danger_level_m, elevation_m,
        drainage_quality, and historical_frequency.

    Returns
    -------
    dict
        {
            "score": float,  # Clamped and rounded to [0.0, 1.0]
            "level": str,    # "Low" | "Moderate" | "High" | "Severe"
            "reason": str    # Explanation of top contributing factor
        }
    """
    # 1. Validation & Extraction
    rainfall_mm = _extract_numeric(zone, "rainfall_mm")
    river_level_m = _extract_numeric(zone, "river_level_m")
    danger_level_m = _extract_numeric(zone, "danger_level_m")
    elevation_m = _extract_numeric(zone, "elevation_m")
    drainage_quality = _extract_numeric(zone, "drainage_quality")
    historical_frequency = _extract_numeric(zone, "historical_frequency")

    if rainfall_mm < 0:
        raise ValueError(f"rainfall_mm cannot be negative, got {rainfall_mm}")

    # 2. Factor Normalization
    # Rainfall: 0 mm -> 0, 300 mm -> 1
    rainfall_factor = clamp(rainfall_mm / 300.0, 0.0, 1.0)

    # River Level relative to danger level: protect against division by zero
    if danger_level_m <= 0:
        river_ratio = 1.2 if river_level_m > 0 else 0.0
    else:
        river_ratio = river_level_m / danger_level_m

    # River ratio: <= 0.5 -> 0, >= 1.2 -> 1
    river_factor = clamp((river_ratio - 0.5) / (1.2 - 0.5), 0.0, 1.0)

    # Historical flood frequency: already 0 to 1
    historical_factor = clamp(historical_frequency, 0.0, 1.0)

    # Elevation: 0 m elevation -> 1 risk, 300 m elevation -> 0 risk
    elevation_factor = clamp(1.0 - (elevation_m / 300.0), 0.0, 1.0)

    # Drainage quality: 0 (poor) -> 1 risk, 1 (excellent) -> 0 risk
    drainage_factor = clamp(1.0 - drainage_quality, 0.0, 1.0)

    # 3. Weighted Contributions
    rainfall_contrib = WEIGHT_RAINFALL * rainfall_factor
    river_contrib = WEIGHT_RIVER * river_factor
    historical_contrib = WEIGHT_HISTORICAL * historical_factor
    elevation_contrib = WEIGHT_ELEVATION * elevation_factor
    drainage_contrib = WEIGHT_DRAINAGE * drainage_factor

    raw_score = (
        rainfall_contrib
        + river_contrib
        + historical_contrib
        + elevation_contrib
        + drainage_contrib
    )
    score = round(clamp(raw_score, 0.0, 1.0), 2)

    # 4. Risk Level Thresholds
    if score < 0.25:
        level = "Low"
    elif score < 0.50:
        level = "Moderate"
    elif score < 0.75:
        level = "High"
    else:
        level = "Severe"

    # 5. Explainable Reason (identifying top weighted contributor)
    contributors = [
        (rainfall_contrib, "Heavy rainfall is the main contributor to the current risk."),
        (river_contrib, "River level close to the danger level is the main contributor to the current risk."),
        (historical_contrib, "High historical flood frequency is the main contributor to the current risk."),
        (elevation_contrib, "Low elevation is the main contributor to the current risk."),
        (drainage_contrib, "Poor drainage is the main contributor to the current risk."),
    ]
    _, reason = max(contributors, key=lambda item: item[0])

    return {
        "score": score,
        "level": level,
        "reason": reason,
    }


def compute_risk_score(
    rainfall_mm: float,
    river_level_m: float,
    danger_level_m: float,
    elevation_m: float,
    drainage_quality: float,
    historical_frequency: float,
) -> float:
    """
    Computes a risk score between 0.0 and 1.0 from raw parameter values.
    Provided for backward compatibility.
    """
    zone_dict = {
        "rainfall_mm": rainfall_mm,
        "river_level_m": river_level_m,
        "danger_level_m": danger_level_m,
        "elevation_m": elevation_m,
        "drainage_quality": drainage_quality,
        "historical_frequency": historical_frequency,
    }
    result = compute_risk(zone_dict)
    return result["score"]


# ---------------------------------------------------------------------------
# Rainfall Simulation Function
# ---------------------------------------------------------------------------

def bump_rainfall(db: Session, zone_id: int, delta: float) -> Optional[Zone]:
    """
    Simulates changing environmental conditions by adjusting a Zone's rainfall_mm.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy database session.
    zone_id : int
        Primary key ID of the Zone to update.
    delta : float
        Amount of rainfall in millimetres to add (positive or negative).

    Returns
    -------
    Zone or None
        The updated Zone object, or None if zone_id does not exist.

    Raises
    ------
    TypeError
        If delta is not numeric.
    ValueError
        If the resulting rainfall_mm would become negative.
    """
    if isinstance(delta, bool) or not isinstance(delta, (int, float)):
        raise TypeError(f"delta must be a numeric value, got {type(delta).__name__}")

    zone = db.query(Zone).filter(Zone.id == zone_id).first()
    if zone is None:
        return None

    new_rainfall = zone.rainfall_mm + delta
    if new_rainfall < 0:
        raise ValueError(
            f"Resulting rainfall cannot be negative (current: {zone.rainfall_mm} mm, delta: {delta} mm)."
        )

    zone.rainfall_mm = new_rainfall
    db.add(zone)
    db.commit()
    db.refresh(zone)
    return zone
