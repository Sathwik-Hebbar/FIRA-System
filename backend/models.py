"""
FIRA – SQLAlchemy ORM models.
Defines the database schema for flood zones, citizen reports, and shelters.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String

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
    """

    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, index=True)

    # --- Reporter details ---
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True)

    # --- Incident location ---
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)

    # --- Incident metadata ---
    severity = Column(Integer, nullable=False)            # 1 (minor) – 5 (catastrophic)
    description = Column(String, nullable=True)

    # --- Computed / assigned fields ---
    priority_score = Column(Float, nullable=True)         # Set by priority_engine
    status = Column(String, default="pending")            # pending | active | resolved
    shelter_id = Column(Integer, ForeignKey("shelters.id"), nullable=True)

    # --- Timestamps ---
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class Shelter(Base):
    """An emergency shelter available during flood events."""

    __tablename__ = "shelters"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    capacity = Column(Integer, nullable=False)
