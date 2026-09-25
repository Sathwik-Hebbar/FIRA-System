"""
FIRA – Risk Engine.
Computes a normalised risk score (0.0 – 1.0) for a flood zone based on
sensor readings and geographic/historical factors.

Score interpretation:
  0.00 – 0.39  → Low risk
  0.40 – 0.69  → Moderate risk
  0.70 – 1.00  → High risk
"""

from __future__ import annotations

from typing import Dict

from sqlalchemy.orm import Session

from models import Zone


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    """Clamp *value* to the range ``[minimum, maximum]``.

    A tiny helper kept explicit for readability and beginner‑friendliness.
    """
    return max(minimum, min(maximum, value))


def _compute_factors(zone: Zone) -> Dict[str, float]:
    """Return the normalised factors for the given ``Zone``.

    The function follows the exact normalisation rules described in the
    specification.
    """
    # Rainfall (0 mm → 0, 300 mm → 1)
    rainfall_factor = _clamp(zone.rainfall_mm / 300.0)

    # River level relative to danger level
    if zone.danger_level_m == 0:
        # Avoid division‑by‑zero – if danger level is unknown we treat the
        # risk as maximal when any river level is present.
        river_factor = 1.0 if zone.river_level_m > 0 else 0.0
    else:
        river_ratio = zone.river_level_m / zone.danger_level_m
        river_factor = _clamp((river_ratio - 0.5) / (1.2 - 0.5))

    # Historical frequency is already 0‑1
    historical_factor = _clamp(zone.historical_frequency)

    # Elevation: lower elevation → higher risk (0 m → 1, 300 m → 0)
    elevation_factor = _clamp(1.0 - (zone.elevation_m / 300.0))

    # Drainage quality: 0 (poor) → 1 risk, 1 (excellent) → 0 risk
    drainage_factor = _clamp(1.0 - zone.drainage_quality)

    return {
        "rainfall": rainfall_factor,
        "river": river_factor,
        "historical": historical_factor,
        "elevation": elevation_factor,
        "drainage": drainage_factor,
    }


def compute_risk(zone: Zone) -> Dict[str, object]:
    """Calculate the flood risk for *zone*.

    Returns a dictionary with three keys:
    ``score`` – float in ``[0.0, 1.0]``
    ``level`` – one of ``"Low"``, ``"Moderate"``, ``"High"``, ``"Severe"``
    ``reason`` – human‑readable sentence describing the main contributing factor.
    """
    # Compute normalised factors
    factors = _compute_factors(zone)

    # Weighted contributions (weights sum to 1.0)
    weighted = {
        "rainfall": 0.35 * factors["rainfall"],
        "river": 0.30 * factors["river"],
        "historical": 0.15 * factors["historical"],
        "elevation": 0.10 * factors["elevation"],
        "drainage": 0.10 * factors["drainage"],
    }

    score = sum(weighted.values())
    score = _clamp(score)

    # Determine risk level
    if score < 0.25:
        level = "Low"
    elif score < 0.50:
        level = "Moderate"
    elif score < 0.75:
        level = "High"
    else:
        level = "Severe"

    # Identify the biggest weighted contribution for the explanatory reason
    main_factor = max(weighted, key=weighted.get)
    reason_map = {
        "rainfall": "Heavy rainfall is the main contributor to the current risk.",
        "river": "River level close to the danger level is the main contributor to the current risk.",
        "historical": "High historical flood frequency is the main contributor to the current risk.",
        "elevation": "Low elevation is the main contributor to the current risk.",
        "drainage": "Poor drainage is the main contributor to the current risk.",
    }
    reason = reason_map.get(main_factor, "Risk evaluation completed.")

    return {"score": score, "level": level, "reason": reason}


def bump_rainfall(db: Session, zone_id: int, delta: float) -> Zone:
    """Increase ``rainfall_mm`` for the zone identified by ``zone_id``.

    * ``delta`` may be positive or negative but the resulting rainfall will never
      become negative – it is clamped at ``0``.
    * The function commits the change, refreshes the ORM object and returns it.
    * Raises ``ValueError`` if the ``zone_id`` does not exist or ``delta`` is not a
      number.
    """
    if not isinstance(delta, (int, float)):
        raise ValueError("delta must be a numeric value")

    zone = db.query(Zone).filter(Zone.id == zone_id).first()
    if zone is None:
        raise ValueError(f"Zone with id {zone_id} not found")

    new_rain = zone.rainfall_mm + float(delta)
    zone.rainfall_mm = max(0.0, new_rain)  # prevent negative rainfall

    db.add(zone)
    db.commit()
    db.refresh(zone)
    return zone
