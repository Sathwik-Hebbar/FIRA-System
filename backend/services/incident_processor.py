"""Authoritative incident pipeline shared by voice and other incident sources.

The LLM may extract/review facts, but all risk and response priority decisions
are deterministic and persisted with the incident.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from models import Report
from schemas.voice_sos import EmergencyExtraction, VoiceCitizenProfile

logger = logging.getLogger("fira.incident_processor")

RISK_WEIGHTS = {
    "trapped": 30, "medical_emergency": 30, "injured": 25,
    "disabled": 20, "elderly": 15, "children": 15,
    "waist": 10, "chest": 20, "above_chest": 30,
    "building_damage": 15, "electricity_danger": 15, "road_blocked": 10,
    "vehicle_trapped": 10, "people_5": 5, "people_10": 10,
}


def _water_category(value: Optional[str]) -> Optional[str]:
    text = (value or "").lower()
    if any(word in text for word in ("chest", "neck", "roof", "above chest", "deep")):
        return "CHEST" if "chest" in text else "ABOVE_CHEST"
    if "waist" in text:
        return "WAIST"
    if "knee" in text:
        return "KNEE"
    if "ankle" in text:
        return "ANKLE"
    return None


def normalize_incident(
    extraction: EmergencyExtraction, citizen: VoiceCitizenProfile, *, source: str,
    raw_text: Optional[str], location: Dict[str, Any], incident_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Map extraction aliases into the sole canonical incident representation."""
    raw = extraction.model_dump()
    notes = (raw.get("additional_information") or "").lower()
    building_condition = raw.get("building_condition") or ""
    return {
        "incident_id": str(incident_id) if incident_id else None,
        "source": source.lower(),
        "citizen": {"name": citizen.name, "phone": citizen.phone, "language": citizen.language},
        "location": {
            "latitude": location.get("lat"), "longitude": location.get("lng"),
            "address": raw.get("location_description"), "location_confidence": 1.0 if location.get("source") != "REGIONAL_FALLBACK" else 0.0,
        },
        "situation": {
            "description": raw_text or "", "water_level": raw.get("water_depth_m"),
            "water_level_category": _water_category(raw.get("water_level")),
            "trapped": bool(raw.get("trapped")), "people_count": raw.get("people_count"),
            "children_count": raw.get("children_count"), "elderly_count": raw.get("elderly_count"),
            "disabled_count": raw.get("disabled_person_count"),
            "injured_count": 1 if raw.get("injured") else None,
            "medical_emergency": bool(raw.get("medical_emergency")),
            "food_needed": "food" in notes, "shelter_needed": "shelter" in notes,
            "electricity_danger": any(x in notes for x in ("live wire", "electricity danger", "electric wire")),
            "building_damage": any(x in building_condition.lower() for x in ("collapse", "crack", "submerg", "damage")),
            "road_blocked": "road blocked" in notes, "vehicle_trapped": "vehicle trapped" in notes,
        },
        "risk": {}, "priority": {},
        "ai_analysis": {"summary": "", "extracted_facts": raw, "missing_information": [], "confidence": 0},
        "status": "NEW",
        "timestamps": {"created_at": datetime.now(timezone.utc).isoformat(), "updated_at": datetime.now(timezone.utc).isoformat()},
    }


def calculate_incident_risk(incident: Dict[str, Any], location_risk: float = 0.0) -> Dict[str, Any]:
    s, reasons = incident["situation"], []
    score = life = medical = infrastructure = vulnerability = 0
    def add(condition: bool, weight: int, label: str, bucket: str = "life"):
        nonlocal score, life, medical, infrastructure, vulnerability
        if condition:
            score += weight; reasons.append(label)
            if bucket == "medical": medical += weight
            elif bucket == "infrastructure": infrastructure += weight
            elif bucket == "vulnerability": vulnerability += weight
            else: life += weight
    add(s["trapped"], RISK_WEIGHTS["trapped"], "Citizen reported being trapped")
    add(s["medical_emergency"], RISK_WEIGHTS["medical_emergency"], "Medical emergency reported", "medical")
    add(bool(s["injured_count"]), RISK_WEIGHTS["injured"], "Injured person reported", "medical")
    add(bool(s["disabled_count"]), RISK_WEIGHTS["disabled"], "Disabled person needs assistance", "vulnerability")
    add(bool(s["elderly_count"]), RISK_WEIGHTS["elderly"], "Elderly person present", "vulnerability")
    add(bool(s["children_count"]), RISK_WEIGHTS["children"], "Child present", "vulnerability")
    category = s["water_level_category"]
    add(category == "WAIST", RISK_WEIGHTS["waist"], "Waist-level water")
    add(category == "CHEST", RISK_WEIGHTS["chest"], "Chest-level water")
    add(category == "ABOVE_CHEST", RISK_WEIGHTS["above_chest"], "Deep/above-chest water")
    add(s["building_damage"], RISK_WEIGHTS["building_damage"], "Building damage", "infrastructure")
    add(s["electricity_danger"], RISK_WEIGHTS["electricity_danger"], "Electricity danger", "infrastructure")
    add(s["road_blocked"], RISK_WEIGHTS["road_blocked"], "Road blocked", "infrastructure")
    add(s["vehicle_trapped"], RISK_WEIGHTS["vehicle_trapped"], "Vehicle trapped", "infrastructure")
    people = s["people_count"] or 0
    add(people >= 5, RISK_WEIGHTS["people_5"], "Five or more people affected")
    add(people >= 10, RISK_WEIGHTS["people_10"], "Ten or more people affected")
    overall = min(100, score)
    level = "LOW" if overall < 25 else "MODERATE" if overall < 50 else "HIGH" if overall < 75 else "CRITICAL"
    return {"flood_risk": min(100, score - life - medical - infrastructure - vulnerability), "life_risk": min(100, life), "medical_risk": min(100, medical), "infrastructure_risk": min(100, infrastructure), "location_risk": round(max(0, min(location_risk, 1)) * 100), "vulnerability_score": min(100, vulnerability), "overall_risk": overall, "level": level, "reasons": reasons}


def calculate_incident_priority(incident: Dict[str, Any], risk: Dict[str, Any]) -> Dict[str, Any]:
    s = incident["situation"]
    score = (0.50 * risk["overall_risk"] + 0.20 * risk["life_risk"] + 0.10 * risk["medical_risk"] + 0.10 * risk["vulnerability_score"] + 0.10 * risk["location_risk"])
    reasons = list(risk["reasons"])
    # Explicit safety overrides are transparent and can only escalate.
    p1 = (s["medical_emergency"] and bool(s["injured_count"])) or (s["trapped"] and s["water_level_category"] in ("CHEST", "ABOVE_CHEST")) or (s["trapped"] and bool((s["children_count"] or 0) + (s["elderly_count"] or 0) + (s["disabled_count"] or 0))) or (s["trapped"] and (s["people_count"] or 0) > 1)
    if p1:
        score = max(score, 75); reasons.append("P1 safety override applied")
    score = round(min(100, score), 2)
    level = "P1 / CRITICAL" if score >= 75 else "P2 / HIGH" if score >= 50 else "P3 / MEDIUM" if score >= 25 else "P4 / LOW"
    return {"score": score, "level": level, "reason": reasons, "confidence": 1.0, "safety_override": p1}


def process_incident(db: Any, *, source: str, raw_input: Any, extraction: EmergencyExtraction,
                     citizen: VoiceCitizenProfile, location: Dict[str, Any], session_id: Optional[str] = None,
                     location_risk: float = 0.0, report: Optional[Report] = None) -> tuple[Report, Dict[str, Any]]:
    """Normalize, score, persist and return a single authoritative incident result."""
    logger.info("INCIDENT_NORMALIZATION_STARTED source=%s session_id=%s", source, session_id)
    normalized = normalize_incident(extraction, citizen, source=source, raw_text=str(raw_input or ""), location=location, incident_id=getattr(report, "id", None))
    risk = calculate_incident_risk(normalized, location_risk)
    priority = calculate_incident_priority(normalized, risk)
    normalized["risk"] = risk
    normalized["priority"] = priority
    now = datetime.now(timezone.utc)
    if report is None:
        report = Report(name=citizen.name or f"Voice Caller ({citizen.phone})", phone=citizen.phone, reported_by=citizen.id,
            lat=location.get("lat") or 12.9716, lng=location.get("lng") or 77.5946, severity=5 if priority["score"] >= 75 else 4 if priority["score"] >= 50 else 3,
            description=str(raw_input or ""), source=source, voice_session_id=session_id, status="prioritized")
        db.add(report); db.flush()
        normalized["incident_id"] = str(report.id)
    report.raw_input = json.dumps(raw_input, default=str)
    report.normalized_data = json.dumps(normalized, default=str)
    report.risk_score = risk["overall_risk"]
    report.risk_level = risk["level"]
    report.priority_score = round(priority["score"] / 100, 3)
    report.priority_reasons = json.dumps(priority["reason"])
    report.ai_analysis = json.dumps(normalized["ai_analysis"])
    report.updated_at = now
    logger.info("RISK_CALCULATED incident_id=%s score=%s", report.id, risk["overall_risk"])
    logger.info("PRIORITY_CALCULATED incident_id=%s score=%s level=%s", report.id, priority["score"], priority["level"])
    return report, normalized
