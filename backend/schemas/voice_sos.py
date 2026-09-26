"""
FIRA – Voice SOS Schemas & Data Contract (Phase 1).
Defines Pydantic models for Voice SOS sessions, AI information extraction,
citizen profile representation, and normalized FIRA SOS incident payloads.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Emergency Information Extracted from Citizen Speech
# ---------------------------------------------------------------------------

class EmergencyExtraction(BaseModel):
    """
    Structured emergency facts extracted from caller speech.
    Unknown or unmentioned fields MUST be null. Never infer or invent facts.
    """
    incident_type: Optional[str] = Field(
        default="FLOOD",
        description="Type of incident: FLOOD, WATERLOGGING, RIVER_OVERFLOW, etc."
    )
    safe: Optional[bool] = Field(
        default=None,
        description="Whether the caller and their group are currently safe from immediate danger."
    )
    trapped: Optional[bool] = Field(
        default=None,
        description="Whether the caller or anyone in their group is physically trapped or unable to leave."
    )
    injured: Optional[bool] = Field(
        default=None,
        description="Whether anyone in the group is injured."
    )
    people_count: Optional[int] = Field(
        default=None,
        description="Total number of people affected or present with the caller."
    )
    children_count: Optional[int] = Field(
        default=None,
        description="Number of children/infants present."
    )
    elderly_count: Optional[int] = Field(
        default=None,
        description="Number of elderly/senior people present."
    )
    disabled_person_count: Optional[int] = Field(
        default=None,
        description="Number of disabled or mobility-impaired individuals."
    )
    medical_emergency: Optional[bool] = Field(
        default=None,
        description="Whether there is an urgent medical condition requiring immediate attention."
    )
    water_level: Optional[str] = Field(
        default=None,
        description="Qualitative water level description (e.g. ankle, knee, waist, chest, roof level)."
    )
    water_depth_m: Optional[float] = Field(
        default=None,
        description="Estimated water depth in metres if mentioned."
    )
    building_condition: Optional[str] = Field(
        default=None,
        description="Condition of the house/structure (e.g. collapsed, cracking, submerged, unstable)."
    )
    evacuation_possible: Optional[bool] = Field(
        default=None,
        description="Whether the group can evacuate on their own without external rescue."
    )
    rescue_required: Optional[bool] = Field(
        default=None,
        description="Whether external rescue team / boat is actively needed."
    )
    location_description: Optional[str] = Field(
        default=None,
        description="Verbal description of location or landmark mentioned by caller."
    )
    additional_information: Optional[str] = Field(
        default=None,
        description="Any specific hazards, power status, or details rescue teams should know."
    )


# ---------------------------------------------------------------------------
# Citizen Profile in Voice SOS Context
# ---------------------------------------------------------------------------

class VoiceCitizenProfile(BaseModel):
    """
    Authoritative citizen profile merged from the FIRA User database.
    """
    id: Optional[int] = None
    name: Optional[str] = None
    phone: str
    language: str = "kn-IN"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    is_verified: bool = False


# ---------------------------------------------------------------------------
# Normalized FIRA SOS Data Contract
# ---------------------------------------------------------------------------

class NormalizedFIRASOS(BaseModel):
    """
    Standard normalized FIRA SOS object ready for the Priority Engine and DB persistence.
    """
    source: str = "VOICE_CALL"
    session_id: str
    citizen_id: Optional[int] = None
    caller_phone: str
    language: str = "kn-IN"
    transcript: Optional[str] = None
    translated_transcript: Optional[str] = None
    citizen: VoiceCitizenProfile
    incident: EmergencyExtraction
    location: Dict[str, Any] = Field(
        default_factory=lambda: {"lat": None, "lng": None, "description": None, "source": "UNKNOWN"}
    )
    priority: Optional[Dict[str, Any]] = None
    status: str = "PENDING_RESCUE"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# API Request & Response Schemas
# ---------------------------------------------------------------------------

class VoiceProcessRequest(BaseModel):
    """
    Request payload to process speech or a transcript via Voice SOS pipeline.
    """
    caller_phone: str
    transcript: Optional[str] = None
    audio_base64: Optional[str] = None
    audio_url: Optional[str] = None
    language: str = "kn-IN"
    session_id: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


class VoiceProcessResponse(BaseModel):
    """
    Response returned by Voice SOS processing.
    """
    success: bool
    sos_id: Optional[int] = None
    session_id: str
    source: str = "VOICE_CALL"
    citizen_id: Optional[int] = None
    citizen_name: Optional[str] = None
    citizen_matched: bool = False
    language: str = "kn-IN"
    transcript: Optional[str] = None
    translated_transcript: Optional[str] = None
    incident: EmergencyExtraction
    priority: Optional[Dict[str, Any]] = None
    assigned_shelter: Optional[str] = None
    emergency_incident: Optional[Dict[str, Any]] = None
    safety_instruction: Optional[str] = None
    response_speech_text: str
    response_audio_base64: Optional[str] = None
    status: str = "PENDING_RESCUE"


class VoiceSessionDetail(BaseModel):
    """
    Detailed session payload for Command Center review.
    """
    id: str
    citizen_id: Optional[int] = None
    caller_phone: str
    language: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    transcript: Optional[str] = None
    translated_transcript: Optional[str] = None
    extracted_data: Optional[Dict[str, Any]] = None
    status: str
    incident_id: Optional[int] = None
