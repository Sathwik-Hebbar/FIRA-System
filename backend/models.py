"""
FIRA – SQLAlchemy ORM models.
Defines the database schema for flood zones, citizen reports, shelters, and users.
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String

from database import Base


class Zone(Base):
    """
    A geographic zone monitored for flood risk.

    Sensor / survey fields are stored as-is; computed scores are written
    back by the risk and priority engines.
    """

    __tablename__ = "zones"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)

    # --- Sensor / environmental readings ---
    rainfall_mm = Column(Float, nullable=False)           # Current rainfall (mm)
    river_level_m = Column(Float, nullable=False)         # Current river level (m)
    danger_level_m = Column(Float, nullable=False)        # Official danger threshold (m)
    elevation_m = Column(Float, nullable=False)           # Zone elevation above sea level (m)
    drainage_quality = Column(Float, nullable=False)      # 0.0 (very poor) – 1.0 (excellent)
    historical_frequency = Column(Float, nullable=False)  # 0.0 (never flooded) – 1.0 (very frequent)


class Report(Base):
    """
    A citizen-submitted flood incident report.

    priority_score is computed by the priority engine after submission.
    shelter_id optionally links the reporter to a recommended shelter.
    reported_by links the report to the submitting User.
    """

    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, index=True)

    # --- Reporter details (cached for display) ---
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True)

    # --- Ownership (FK to users.id) ---
    reported_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # --- Incident location ---
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)

    # --- Incident metadata ---
    severity = Column(Integer, nullable=False)            # 1 (minor) – 5 (catastrophic)
    description = Column(String, nullable=True)

    # --- Extended triage fields ---
    flood_type = Column(String, nullable=True)            # e.g. river / urban / flash
    water_depth_m = Column(Float, nullable=True)
    people_affected = Column(Integer, nullable=True)
    people_trapped = Column(Integer, nullable=True)
    medical_emergency = Column(Boolean, nullable=True)
    photo_url = Column(String, nullable=True)

    # --- Computed / assigned fields ---
    priority_score = Column(Float, nullable=True)         # Set by priority_engine
    # Canonical incident-processing audit trail. JSON is stored as text
    # while retaining the complete payload in Neon PostgreSQL.
    raw_input = Column(String, nullable=True)
    normalized_data = Column(String, nullable=True)
    risk_score = Column(Float, nullable=True)
    risk_level = Column(String, nullable=True)
    priority_reasons = Column(String, nullable=True)
    ai_analysis = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    # Workflow status: reported → prioritized → assigned → in_progress → resolved
    status = Column(String, default="reported")
    shelter_id = Column(Integer, ForeignKey("shelters.id"), nullable=True)
    assigned_resource = Column(String, nullable=True)     # free-text resource name

    # --- Channel & Voice Integration ---
    source = Column(String, default="WEB", nullable=False)  # 'WEB', 'MOBILE', 'VOICE_CALL'
    voice_session_id = Column(String, ForeignKey("voice_sessions.id"), nullable=True)

    # --- Timestamps ---
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    resolved_at = Column(DateTime(timezone=True), nullable=True)


class VoiceSession(Base):
    """
    An emergency voice call session.
    Maintains caller metadata, language, timestamps, raw & translated transcripts,
    extracted facts, conversation state, and linkage to the created Report incident.
    """

    __tablename__ = "voice_sessions"

    id = Column(String, primary_key=True, index=True)  # UUID or telephony Call SID
    citizen_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    caller_phone = Column(String, nullable=False, index=True)
    language = Column(String, default="kn-IN", nullable=False)

    started_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    ended_at = Column(DateTime(timezone=True), nullable=True)

    transcript = Column(String, nullable=True)
    translated_transcript = Column(String, nullable=True)
    extracted_data = Column(String, nullable=True)  # JSON-serialized EmergencyExtraction
    raw_payload = Column(String, nullable=True)  # Original webhook/API payload for safe debugging
    conversation_history = Column(String, nullable=True)  # JSON-serialized message history
    status = Column(String, default="VOICE_SESSION_STARTED", nullable=False)
    incident_id = Column(Integer, ForeignKey("reports.id"), nullable=True)


class Shelter(Base):
    """An emergency shelter available during flood events."""

    __tablename__ = "shelters"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    capacity = Column(Integer, nullable=False)


class User(Base):
    """Application user with authentication details and role.

    Roles are limited to 'citizen' and 'command_center' and are stored in the database.
    Passwords are stored as a securely hashed value using bcrypt.
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False)  # 'citizen' or 'command_center'
    phone = Column(String, nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
