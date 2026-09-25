"""
FIRA – Priority Engine (Step 5).
Computes a transparent, explainable, rule-based responder priority score (0–100)
for citizen SOS reports by evaluating self-reported severity, emergency text signals,
vulnerability indicators, and zone flood risk.

Priority Score Range: 0.0 to 100.0
"""

import re
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Centralized Weight Configurations
# ---------------------------------------------------------------------------

SEVERITY_WEIGHTS: Dict[str, float] = {
    "Low": 15.0,
    "Medium": 30.0,
    "High": 45.0,
    "Critical": 55.0,
}

EMERGENCY_SIGNALS: Dict[str, List[str]] = {
    "trapped": [
        "trapped",
        "can't get out",
        "cant get out",
        "cannot get out",
        "unable to leave",
    ],
    "stuck": [
        "stuck",
        "stranded",
        "blocked",
    ],
    "water_rising": [
        "water rising",
        "water is rising",
        "water level increasing",
        "flood water increasing",
    ],
    "cannot_swim": [
        "can't swim",
        "cant swim",
        "cannot swim",
        "unable to swim",
    ],
    "no_power": [
        "no power",
        "power outage",
        "electricity gone",
        "blackout",
    ],
}

EMERGENCY_SIGNAL_WEIGHTS: Dict[str, float] = {
    "trapped": 15.0,
    "stuck": 10.0,
    "water_rising": 15.0,
    "cannot_swim": 15.0,
    "no_power": 5.0,
}

VULNERABILITY_SIGNALS: Dict[str, List[str]] = {
    "child": [
        "child",
        "children",
        "kid",
        "kids",
        "baby",
        "infant",
    ],
    "elderly": [
        "elderly",
        "senior",
        "old person",
        "aged",
    ],
    "disabled": [
        "disabled",
        "wheelchair",
        "mobility issue",
        "cannot walk",
    ],
}

VULNERABILITY_SIGNAL_WEIGHTS: Dict[str, float] = {
    "child": 10.0,
    "elderly": 10.0,
    "disabled": 10.0,
}

# Human-readable labels used in explainability reasons
SIGNAL_REASON_LABELS: Dict[str, str] = {
    "trapped": "trapped person",
    "stuck": "stranded individual",
    "water_rising": "rising water",
    "cannot_swim": "person unable to swim",
    "no_power": "power outage",
    "child": "children present",
    "elderly": "elderly person",
    "disabled": "mobility-impaired/disabled individual",
}


# ---------------------------------------------------------------------------
# Signal Extraction Helper
# ---------------------------------------------------------------------------

def extract_signals(description: Optional[str]) -> Tuple[List[str], List[str]]:
    """
    Extracts distinct emergency and vulnerability signals from free-text description.

    Uses deterministic word-boundary phrase matching.
    Each distinct signal key is counted at most once regardless of occurrence frequency.

    Parameters
    ----------
    description : str or None
        Free-text description from citizen SOS report.

    Returns
    -------
    tuple of (list of str, list of str)
        (detected_emergency_signals, detected_vulnerability_signals)
    """
    if not description:
        return [], []

    # Normalize text: lowercase, replace curly apostrophes, strip whitespace
    normalized = description.replace("’", "'").replace("`", "'").lower().strip()

    detected_emergency: List[str] = []
    for signal_name, phrases in EMERGENCY_SIGNALS.items():
        for phrase in phrases:
            pattern = r"\b" + re.escape(phrase) + r"\b"
            if re.search(pattern, normalized, re.IGNORECASE):
                detected_emergency.append(signal_name)
                break  # Count distinct signal only once

    detected_vulnerability: List[str] = []
    for signal_name, phrases in VULNERABILITY_SIGNALS.items():
        for phrase in phrases:
            pattern = r"\b" + re.escape(phrase) + r"\b"
            if re.search(pattern, normalized, re.IGNORECASE):
                detected_vulnerability.append(signal_name)
                break  # Count distinct signal only once

    return detected_emergency, detected_vulnerability


# ---------------------------------------------------------------------------
# Explainability Helper
# ---------------------------------------------------------------------------

def _build_reason(
    severity: str,
    emergency_signals: List[str],
    vulnerability_signals: List[str],
    zone_risk_score: float,
) -> str:
    """Builds a human-readable explanation for the computed priority score."""
    elements: List[str] = []

    for sig in emergency_signals:
        if sig in SIGNAL_REASON_LABELS:
            elements.append(SIGNAL_REASON_LABELS[sig])

    for sig in vulnerability_signals:
        if sig in SIGNAL_REASON_LABELS:
            elements.append(SIGNAL_REASON_LABELS[sig])

    # Zone flood risk descriptor
    if zone_risk_score >= 0.50:
        elements.append("high-risk flood zone")
    elif zone_risk_score >= 0.25:
        elements.append("moderate-risk flood zone")
    elif zone_risk_score > 0.0:
        elements.append("low-risk flood zone")

    if not elements:
        return f"{severity} self-reported severity with no emergency or vulnerability signals detected."

    if len(elements) == 1:
        return f"{severity} self-reported severity with {elements[0]}."
    elif len(elements) == 2:
        return f"{severity} self-reported severity with {elements[0]} and {elements[1]}."
    else:
        return f"{severity} self-reported severity with {', '.join(elements[:-1])}, and {elements[-1]}."


# ---------------------------------------------------------------------------
# Main Priority Computation Function
# ---------------------------------------------------------------------------

def compute_priority(
    severity: str,
    description: Optional[str] = None,
    zone_risk_score: float = 0.0,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Computes an explainable priority score (0–100) for a citizen SOS report.

    Parameters
    ----------
    severity : str
        Self-reported severity: "Low", "Medium", "High", or "Critical" (case-insensitive).
    description : str or None, optional
        Free-text description of the incident.
    zone_risk_score : float, optional
        Normalized flood risk score for the zone (0.0 to 1.0). Default is 0.0.
    lat : float or None, optional
        Incident latitude (-90.0 to 90.0).
    lng : float or None, optional
        Incident longitude (-180.0 to 180.0).

    Returns
    -------
    dict
        Structured result containing:
        - score (float: 0.0 - 100.0)
        - severity (str: normalized severity)
        - severity_score (float)
        - emergency_signals (list of str)
        - emergency_signal_score (float)
        - vulnerability_signals (list of str)
        - vulnerability_signal_score (float)
        - zone_risk_score (float)
        - zone_risk_contribution (float)
        - reason (str)
        - lat (float, optional if provided)
        - lng (float, optional if provided)
    """
    # 1. Validate Severity
    if severity is None or not isinstance(severity, str):
        raise ValueError(f"Severity must be a valid string, got: {severity!r}")

    normalized_severity = severity.strip().capitalize()
    if normalized_severity not in SEVERITY_WEIGHTS:
        valid_options = ", ".join(SEVERITY_WEIGHTS.keys())
        raise ValueError(
            f"Invalid severity: '{severity}'. Must be one of: {valid_options}."
        )

    severity_score = SEVERITY_WEIGHTS[normalized_severity]

    # 2. Validate Zone Risk Score
    if isinstance(zone_risk_score, bool) or not isinstance(zone_risk_score, (int, float)):
        raise TypeError(
            f"zone_risk_score must be a numeric value, got {type(zone_risk_score).__name__}"
        )

    if zone_risk_score < 0.0 or zone_risk_score > 1.0:
        raise ValueError(
            f"zone_risk_score must be between 0.0 and 1.0, got {zone_risk_score}"
        )

    # 3. Validate Location Coordinates (if provided)
    if lat is not None:
        if isinstance(lat, bool) or not isinstance(lat, (int, float)):
            raise TypeError(f"lat must be a numeric value, got {type(lat).__name__}")
        if not (-90.0 <= float(lat) <= 90.0):
            raise ValueError(f"Latitude must be between -90 and 90, got {lat}")

    if lng is not None:
        if isinstance(lng, bool) or not isinstance(lng, (int, float)):
            raise TypeError(f"lng must be a numeric value, got {type(lng).__name__}")
        if not (-180.0 <= float(lng) <= 180.0):
            raise ValueError(f"Longitude must be between -180 and 180, got {lng}")

    # 4. Extract Text Signals
    detected_emergency, detected_vulnerability = extract_signals(description)

    # 5. Calculate Sub-Scores
    emergency_signal_score = sum(
        EMERGENCY_SIGNAL_WEIGHTS[sig] for sig in detected_emergency
    )
    vulnerability_signal_score = sum(
        VULNERABILITY_SIGNAL_WEIGHTS[sig] for sig in detected_vulnerability
    )

    # Zone risk contribution: bounded up to 15.0
    zone_risk_contribution = round(min(zone_risk_score * 15.0, 15.0), 2)

    # 6. Final Priority Score (capped between 0.0 and 100.0)
    raw_final_score = (
        severity_score
        + emergency_signal_score
        + vulnerability_signal_score
        + zone_risk_contribution
    )
    final_score = round(max(0.0, min(raw_final_score, 100.0)), 2)

    # 7. Generate Explainable Reason
    reason = _build_reason(
        severity=normalized_severity,
        emergency_signals=detected_emergency,
        vulnerability_signals=detected_vulnerability,
        zone_risk_score=zone_risk_score,
    )

    result: Dict[str, Any] = {
        "score": final_score,
        "severity": normalized_severity,
        "severity_score": round(severity_score, 2),
        "emergency_signals": detected_emergency,
        "emergency_signal_score": round(emergency_signal_score, 2),
        "vulnerability_signals": detected_vulnerability,
        "vulnerability_signal_score": round(vulnerability_signal_score, 2),
        "zone_risk_score": round(float(zone_risk_score), 2),
        "zone_risk_contribution": zone_risk_contribution,
        "reason": reason,
    }

    if lat is not None:
        result["lat"] = float(lat)
    if lng is not None:
        result["lng"] = float(lng)

    return result


# ---------------------------------------------------------------------------
# Zone Ranking Helper (Kept for compatibility with triage workflows)
# ---------------------------------------------------------------------------

def rank_zones(zones: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
    sorted_zones = sorted(zones, key=lambda z: z.get("risk_score", 0.0), reverse=True)
    for rank, zone in enumerate(sorted_zones, start=1):
        zone["priority_rank"] = rank
    return sorted_zones


# ---------------------------------------------------------------------------
# Helpers for API and Report Model Integration
# ---------------------------------------------------------------------------

def priority_level(score: Optional[float]) -> str:
    """Convert a numeric priority score (0–1 or 0–100) to a human-readable level."""
    if score is None:
        return "Low"
    # If score is on 0-100 scale:
    if score > 1.0:
        if score >= 75.0:
            return "Critical"
        if score >= 50.0:
            return "High"
        if score >= 25.0:
            return "Moderate"
        return "Low"
    # If score is on 0-1 scale:
    if score >= 0.75:
        return "Critical"
    if score >= 0.50:
        return "High"
    if score >= 0.25:
        return "Moderate"
    return "Low"


def compute_report_priority(report: Any, env_risk: float = 0.0) -> float:
    """
    Computes a normalized priority score (0.0 – 1.0) for a Report ORM model or dict,
    leveraging the explainable multi-signal Priority Engine and triage fields.

    Parameters
    ----------
    report : Report or dict
        Incident report record.
    env_risk : float
        Environmental zone risk score (0.0 to 1.0).

    Returns
    -------
    float
        Normalized priority score clamped to [0.0, 1.0].
    """
    # 1. Map severity (integer 1-5 or string)
    raw_sev = getattr(report, "severity", None)
    if isinstance(report, dict) and raw_sev is None:
        raw_sev = report.get("severity")

    severity_map = {
        1: "Low",
        2: "Low",
        3: "Medium",
        4: "High",
        5: "Critical",
    }
    if isinstance(raw_sev, (int, float)):
        sev_str = severity_map.get(int(raw_sev), "Medium")
    elif isinstance(raw_sev, str):
        sev_str = raw_sev
    else:
        sev_str = "Medium"

    # 2. Extract description and synthesize triage signals into description if present
    desc = getattr(report, "description", None)
    if isinstance(report, dict) and desc is None:
        desc = report.get("description")
    desc = desc or ""

    extra_tokens: List[str] = []
    if getattr(report, "medical_emergency", False):
        extra_tokens.append("medical emergency")
    trapped_count = getattr(report, "people_trapped", 0) or 0
    if trapped_count > 0:
        extra_tokens.append(f"{trapped_count} people trapped cannot get out")
    affected_count = getattr(report, "people_affected", 0) or 0
    if affected_count > 10:
        extra_tokens.append("many people stranded")

    full_desc = f"{desc} {' '.join(extra_tokens)}".strip()

    lat = getattr(report, "lat", None)
    lng = getattr(report, "lng", None)
    if isinstance(report, dict):
        lat = lat or report.get("lat")
        lng = lng or report.get("lng")

    # 3. Call core explainable priority engine
    p_result = compute_priority(
        severity=sev_str,
        description=full_desc,
        zone_risk_score=env_risk,
        lat=lat,
        lng=lng,
    )

    # 4. Return normalized float in [0.0, 1.0] for the Report model / dashboard stats
    return round(p_result["score"] / 100.0, 3)

