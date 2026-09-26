"""Regression tests for the canonical deterministic incident pipeline."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from schemas.voice_sos import EmergencyExtraction, VoiceCitizenProfile
from services.incident_processor import calculate_incident_priority, calculate_incident_risk, normalize_incident


def _incident(**fields):
    extraction = EmergencyExtraction(**fields)
    citizen = VoiceCitizenProfile(phone="+919999999999", language="en-IN")
    return normalize_incident(extraction, citizen, source="VOICE_CALL", raw_text="test", location={"lat": 12.9, "lng": 77.5, "source": "GPS"})


def _scored(**fields):
    incident = _incident(**fields)
    risk = calculate_incident_risk(incident, 0.0)
    return risk, calculate_incident_priority(incident, risk)


def test_ankle_water_is_low_priority():
    risk, priority = _scored(people_count=2, water_level="ankle")
    assert risk["level"] == "LOW"
    assert priority["level"] == "P4 / LOW"


def test_trapped_people_in_chest_water_are_p1():
    risk, priority = _scored(trapped=True, people_count=5, water_level="chest")
    assert risk["level"] in {"HIGH", "CRITICAL"}
    assert priority["level"] == "P1 / CRITICAL"
    assert "P1 safety override applied" in priority["reason"]


def test_medical_injury_is_p1():
    _, priority = _scored(medical_emergency=True, injured=True, children_count=1)
    assert priority["level"] == "P1 / CRITICAL"


def test_priority_recalculates_from_current_normalized_facts():
    incident = _incident(people_count=2, water_level="ankle")
    low_risk = calculate_incident_risk(incident)
    incident["situation"]["trapped"] = True
    incident["situation"]["water_level_category"] = "CHEST"
    high_risk = calculate_incident_risk(incident)
    assert high_risk["overall_risk"] > low_risk["overall_risk"]
