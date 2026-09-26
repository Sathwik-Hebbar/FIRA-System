"""
FIRA – Voice SOS Merger Service (Phase 6).
Merges authoritative citizen database profiles, extracted emergency facts,
and voice session metadata into a strictly validated NormalizedFIRASOS object.

Rules:
- Database citizen identity is authoritative.
- AI extraction describes current incident only.
- Never overwrite permanent citizen info with AI extractions.
- Never overwrite verified database fields with null.
- Preserve original transcripts and language tags.
- Compute initial triage severity (1-5) deterministically based on facts.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
import uuid

from schemas.voice_sos import (
    EmergencyExtraction,
    NormalizedFIRASOS,
    VoiceCitizenProfile,
)


def calculate_derived_severity(incident: EmergencyExtraction) -> int:
    """
    Deterministically computes a 1-5 severity rating based on extracted emergency facts.
    The Priority Engine will subsequently use this severity along with signals and zone risk.

    1: Minor / Informational
    2: Low risk (group safe, water receding)
    3: Moderate (water rising, unable to evacuate)
    4: High (trapped, vulnerable individuals present)
    5: Critical (medical emergency, injured persons, or children/elderly trapped)
    """
    # Critical cases
    if incident.medical_emergency:
        return 5
    if incident.trapped and (incident.injured or (incident.children_count and incident.children_count > 0)):
        return 5
    if incident.building_condition and any(w in incident.building_condition.lower() for w in ["collaps", "crack", "submerge", "sink"]):
        return 5

    # High severity
    if incident.trapped or incident.injured or incident.rescue_required:
        return 4
    if incident.safe is False and (incident.elderly_count or incident.children_count):
        return 4

    # Moderate severity
    if incident.safe is False or incident.evacuation_possible is False:
        return 3
    if incident.water_level and any(w in incident.water_level.lower() for w in ["waist", "chest", "neck", "roof"]):
        return 3

    # Low / Default
    if incident.safe is True:
        return 2

    return 3


def merge_voice_sos(
    citizen: VoiceCitizenProfile,
    extraction: EmergencyExtraction,
    session_id: Optional[str] = None,
    transcript: Optional[str] = None,
    translated_transcript: Optional[str] = None,
    language: str = "kn-IN",
    telephony_lat: Optional[float] = None,
    telephony_lng: Optional[float] = None,
) -> NormalizedFIRASOS:
    """
    Merges citizen profile, emergency extraction, and session metadata into
    a NormalizedFIRASOS data contract.
    """
    sid = session_id or f"voice_{uuid.uuid4().hex[:12]}"

    # Resolve location:
    # 1. Telephony/device GPS if explicitly provided
    # 2. Authoritative citizen home address coordinates from DB
    # 3. Default fallback coordinates (e.g. Bangalore center or null)
    lat: Optional[float] = None
    lng: Optional[float] = None
    loc_source = "UNKNOWN"

    if telephony_lat is not None and telephony_lng is not None:
        lat = telephony_lat
        lng = telephony_lng
        loc_source = "TELEPHONY_GPS"
    elif citizen.latitude is not None and citizen.longitude is not None:
        lat = citizen.latitude
        lng = citizen.longitude
        loc_source = "CITIZEN_PROFILE"
    else:
        # Default monitored area reference (Bangalore central coordinates)
        lat = 12.9716
        lng = 77.5946
        loc_source = "REGIONAL_FALLBACK"

    location_data: Dict[str, Any] = {
        "lat": lat,
        "lng": lng,
        "description": extraction.location_description,
        "source": loc_source,
    }

    normalized = NormalizedFIRASOS(
        source="VOICE_CALL",
        session_id=sid,
        citizen_id=citizen.id,
        caller_phone=citizen.phone,
        language=language,
        transcript=transcript,
        translated_transcript=translated_transcript,
        citizen=citizen,
        incident=extraction,
        location=location_data,
        status="PENDING_RESCUE",
        timestamp=datetime.now(timezone.utc),
    )

    return normalized
