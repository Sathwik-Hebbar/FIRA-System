"""
FIRA – Flood Intelligence & Response Application.
Main FastAPI application entry point.

Combines the complete Authentication System (JWT, bcrypt, role-based access)
with the explainable Risk Scoring Engine and Multi-Signal Priority Engine.
"""

import json
import logging
import math
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

# Ensure backend directory is in sys.path
_backend_dir = str(Path(__file__).resolve().parent)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

# Load project configuration before importing services that capture API keys
# when their singleton clients are constructed.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from database import Base, SessionLocal, engine, ensure_schema_migrations, get_db
from models import Report, Shelter, User, VoiceSession, Zone
from priority_engine import compute_priority, compute_report_priority, priority_level
from risk_engine import bump_rainfall, compute_risk
from routes_voice import router as voice_router
from schemas.voice_sos import EmergencyExtraction, VoiceCitizenProfile
from services.incident_processor import process_incident
from security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    authenticate_user,
    create_access_token,
    get_current_user,
    get_password_hash,
    get_user_by_email,
    require_role,
)
from seed_data import seed_if_empty

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan: DB initialization & initial seed
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FIRA startup: initialising database and seed data…")
    Base.metadata.create_all(bind=engine)
    ensure_schema_migrations()

    db = SessionLocal()
    try:
        seed_if_empty(db)
    finally:
        db.close()

    logger.info("FIRA startup complete.")
    yield
    logger.info("FIRA shutdown.")


# ---------------------------------------------------------------------------
# FastAPI App & Middleware
# ---------------------------------------------------------------------------

app = FastAPI(
    title="FIRA API",
    description="Flood Intelligence & Response Application backend with Authentication.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_no_cache_header(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path.lower()
    if path.endswith((".html", ".css", ".js")) or path in ("/", ""):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# Register Voice SOS Router
app.include_router(voice_router)


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    role: str
    phone: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    class Config:
        from_attributes = True


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class RegisterRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: Optional[str] = "citizen"
    phone: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class ReportCreate(BaseModel):
    lat: float
    lng: float
    severity: int                         # 1 (minor) – 5 (catastrophic)
    description: Optional[str] = None
    flood_type: Optional[str] = None
    water_depth_m: Optional[float] = None
    people_affected: Optional[int] = None
    people_trapped: Optional[int] = None
    medical_emergency: Optional[bool] = None
    photo_url: Optional[str] = None


class ReportResponse(BaseModel):
    id: int
    name: str
    phone: Optional[str] = None
    reported_by: Optional[int] = None
    lat: float
    lng: float
    severity: int
    description: Optional[str] = None
    flood_type: Optional[str] = None
    water_depth_m: Optional[float] = None
    people_affected: Optional[int] = None
    people_trapped: Optional[int] = None
    medical_emergency: Optional[bool] = None
    priority_score: Optional[float] = None
    priority_level: Optional[str] = None
    status: str
    shelter_id: Optional[int] = None
    shelter_name: Optional[str] = None
    assigned_resource: Optional[str] = None
    photo_url: Optional[str] = None
    source: Optional[str] = "WEB"
    voice_session_id: Optional[str] = None
    voice_details: Optional[Dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True


class StatusPatch(BaseModel):
    status: str


class ActionRequest(BaseModel):
    action: Literal["assign", "in_progress", "resolved"]
    resource: Optional[str] = None


class SimulateRainRequest(BaseModel):
    zone_id: int
    delta: float


WORKFLOW_STATUSES = ("reported", "prioritized", "assigned", "in_progress", "resolved")


def _advance_report(report: Report, next_status: str, resource: Optional[str] = None) -> None:
    if report.status not in WORKFLOW_STATUSES:
        report.status = "reported"

    current_index = WORKFLOW_STATUSES.index(report.status)
    next_index = WORKFLOW_STATUSES.index(next_status)
    if next_index != current_index + 1:
        expected_status = (
            WORKFLOW_STATUSES[current_index + 1]
            if current_index + 1 < len(WORKFLOW_STATUSES)
            else "no further status"
        )
        raise HTTPException(
            status_code=409,
            detail=f"Incident must move from {report.status} to {expected_status}",
        )
    if next_status == "assigned" and not resource:
        raise HTTPException(status_code=400, detail="A response resource is required for assignment")
    report.status = next_status
    if resource:
        report.assigned_resource = resource
    if next_status == "resolved":
        report.resolved_at = datetime.now(timezone.utc)


def _nearest_shelter(db: Session, lat: float, lng: float) -> Optional[Shelter]:
    shelters = db.query(Shelter).all()
    if not shelters:
        return None
    return min(
        shelters,
        key=lambda s: math.hypot(s.lat - lat, s.lng - lng),
    )


def _enrich_report(report: Report, db: Session) -> dict:
    shelter_name = None
    if report.shelter_id:
        sh = db.query(Shelter).filter(Shelter.id == report.shelter_id).first()
        shelter_name = sh.name if sh else None

    voice_details = None
    v_sid = getattr(report, "voice_session_id", None)
    if v_sid:
        vs = db.query(VoiceSession).filter(VoiceSession.id == v_sid).first()
        if vs:
            extracted = None
            if vs.extracted_data:
                try:
                    extracted = json.loads(vs.extracted_data)
                except Exception:
                    pass
            voice_details = {
                "session_id": vs.id,
                "language": vs.language,
                "transcript": vs.transcript,
                "translated_transcript": vs.translated_transcript,
                "extracted_data": extracted,
                "status": vs.status,
            }

    return {
        "id": report.id,
        "name": report.name,
        "phone": report.phone,
        "reported_by": report.reported_by,
        "lat": report.lat,
        "lng": report.lng,
        "severity": report.severity,
        "description": report.description,
        "flood_type": report.flood_type,
        "water_depth_m": report.water_depth_m,
        "people_affected": report.people_affected,
        "people_trapped": report.people_trapped,
        "medical_emergency": report.medical_emergency,
        "priority_score": report.priority_score,
        "priority_level": priority_level(report.priority_score) if report.priority_score is not None else None,
        "risk_score": getattr(report, "risk_score", None),
        "risk_level": getattr(report, "risk_level", None),
        "priority_reasons": json.loads(report.priority_reasons) if getattr(report, "priority_reasons", None) else [],
        "normalized_incident": json.loads(report.normalized_data) if getattr(report, "normalized_data", None) else None,
        "status": report.status,
        "shelter_id": report.shelter_id,
        "shelter_name": shelter_name,
        "assigned_resource": report.assigned_resource,
        "photo_url": report.photo_url,
        "source": getattr(report, "source", "WEB") or "WEB",
        "voice_session_id": v_sid,
        "voice_details": voice_details,
        "created_at": report.created_at,
    }


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@app.get("/api/health")
def api_health():
    return {"message": "FIRA API is running.", "version": "1.0.0"}


# ---------------------------------------------------------------------------
# Auth Routes (/auth/login, /auth/register, /auth/me)
# ---------------------------------------------------------------------------

auth_router = APIRouter()


@auth_router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = authenticate_user(db, req.email, req.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token(
        data={"sub": user.email, "role": user.role, "user_id": user.id},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return LoginResponse(access_token=token, user=UserResponse.from_orm(user))


@auth_router.post("/register", response_model=LoginResponse)
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    existing = get_user_by_email(db, req.email)
    if existing:
        raise HTTPException(status_code=400, detail="Email is already registered")

    role = req.role if req.role in ("citizen", "command_center") else "citizen"
    user = User(
        name=req.name,
        email=req.email,
        password_hash=get_password_hash(req.password),
        role=role,
        phone=req.phone,
        latitude=req.latitude,
        longitude=req.longitude,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(
        data={"sub": user.email, "role": user.role, "user_id": user.id},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return LoginResponse(access_token=token, user=UserResponse.from_orm(user))


@auth_router.get("/me", response_model=UserResponse)
def read_me(current_user: User = Depends(get_current_user)):
    return UserResponse.from_orm(current_user)


app.include_router(auth_router, prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Zone & Flood Risk Routes (/zones, /risk/{id}, /simulate-rain)
# ---------------------------------------------------------------------------

@app.get("/zones")
def list_zones(db: Session = Depends(get_db)):
    zones = db.query(Zone).all()
    return [
        {
            "id": z.id,
            "name": z.name,
            "lat": z.lat,
            "lng": z.lng,
            "rainfall_mm": z.rainfall_mm,
            "river_level_m": z.river_level_m,
            "danger_level_m": z.danger_level_m,
            "elevation_m": z.elevation_m,
            "drainage_quality": z.drainage_quality,
            "historical_frequency": z.historical_frequency,
        }
        for z in zones
    ]


@app.get("/zones/{zone_id}")
def get_zone(zone_id: int, db: Session = Depends(get_db)):
    z = db.query(Zone).filter(Zone.id == zone_id).first()
    if not z:
        raise HTTPException(status_code=404, detail="Zone not found")
    return {
        "id": z.id,
        "name": z.name,
        "lat": z.lat,
        "lng": z.lng,
        "rainfall_mm": z.rainfall_mm,
        "river_level_m": z.river_level_m,
        "danger_level_m": z.danger_level_m,
        "elevation_m": z.elevation_m,
        "drainage_quality": z.drainage_quality,
        "historical_frequency": z.historical_frequency,
    }


@app.get("/risk/{zone_id}")
def get_risk(zone_id: int, db: Session = Depends(get_db)):
    z = db.query(Zone).filter(Zone.id == zone_id).first()
    if not z:
        raise HTTPException(status_code=404, detail="Zone not found")
    risk = compute_risk(z)
    return {
        "zone_id": zone_id,
        "zone_name": z.name,
        "score": risk["score"],
        "level": risk["level"],
        "reason": risk["reason"],
        "rainfall_mm": z.rainfall_mm,
        "river_level_m": z.river_level_m,
        "danger_level_m": z.danger_level_m,
    }


@app.post("/simulate-rain")
def simulate_rain(
    req: SimulateRainRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_role("command_center")),
):
    try:
        zone = bump_rainfall(db, req.zone_id, req.delta)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if zone is None:
        raise HTTPException(status_code=404, detail="Zone not found")

    risk = compute_risk(zone)
    return {"id": zone.id, "name": zone.name, "rainfall_mm": zone.rainfall_mm, "risk": risk}


# ---------------------------------------------------------------------------
# Shelters (/shelters)
# ---------------------------------------------------------------------------

@app.get("/shelters")
def list_shelters(db: Session = Depends(get_db)):
    shelters = db.query(Shelter).all()
    return [
        {"id": s.id, "name": s.name, "lat": s.lat, "lng": s.lng, "capacity": s.capacity}
        for s in shelters
    ]


# ---------------------------------------------------------------------------
# Incidents / Reports (/report, /incidents, /incident/{id})
# ---------------------------------------------------------------------------

@app.post("/report")
def create_report(
    req: ReportCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Submit a flood incident report.
    Requires authentication. Sets reported_by automatically from token.
    """
    if current_user.role not in ("citizen", "command_center"):
        raise HTTPException(status_code=403, detail="Authentication required to submit reports")

    # Find nearest zone to compute environmental risk
    zones = db.query(Zone).all()
    if zones:
        nearest_zone = min(zones, key=lambda z: math.hypot(z.lat - req.lat, z.lng - req.lng))
        env_risk = compute_risk(nearest_zone)["score"]
    else:
        env_risk = 0.5

    report = Report(
        name=current_user.name,
        phone=current_user.phone,
        reported_by=current_user.id,
        lat=req.lat,
        lng=req.lng,
        severity=req.severity,
        description=req.description,
        flood_type=req.flood_type,
        water_depth_m=req.water_depth_m,
        people_affected=req.people_affected,
        people_trapped=req.people_trapped,
        medical_emergency=req.medical_emergency,
        photo_url=req.photo_url,
        status="reported",
    )
    db.add(report)
    db.flush()

    # Web reports use the same persisted canonical pipeline as Voice SOS.
    process_incident(
        db, source="WEB", raw_input=req.model_dump(),
        extraction=EmergencyExtraction(incident_type=req.flood_type or "FLOOD", trapped=bool(req.people_trapped), people_count=req.people_affected, medical_emergency=req.medical_emergency, water_depth_m=req.water_depth_m),
        citizen=VoiceCitizenProfile(id=current_user.id, name=current_user.name, phone=current_user.phone or "UNKNOWN", language="en-IN", latitude=req.lat, longitude=req.lng, is_verified=True),
        location={"lat": req.lat, "lng": req.lng, "source": "WEB_GPS"}, location_risk=env_risk, report=report,
    )
    report.status = "prioritized"

    # Assign nearest shelter
    shelter = _nearest_shelter(db, req.lat, req.lng)
    if shelter:
        report.shelter_id = shelter.id

    db.commit()
    db.refresh(report)

    return _enrich_report(report, db)


@app.get("/incidents")
def list_incidents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List incidents:
    - Citizens see their own submitted reports.
    - Command center sees all incidents sorted by priority (highest first).
    """
    if current_user.role == "citizen":
        reports = (
            db.query(Report)
            .filter(Report.reported_by == current_user.id)
            .order_by(Report.created_at.desc())
            .all()
        )
    elif current_user.role == "command_center":
        reports = (
            db.query(Report)
            .order_by(Report.priority_score.desc().nullslast(), Report.created_at.desc())
            .all()
        )
    else:
        raise HTTPException(status_code=403, detail="Known application role required")
    return [_enrich_report(r, db) for r in reports]


@app.get("/incident/{report_id}")
def get_incident(
    report_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    report = db.query(Report).filter(Report.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Incident not found")

    if current_user.role == "citizen" and report.reported_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    if current_user.role not in ("citizen", "command_center"):
        raise HTTPException(status_code=403, detail="Known application role required")

    return _enrich_report(report, db)


@app.patch("/incident/{report_id}")
def patch_incident_status(
    report_id: int,
    body: StatusPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update report workflow status. Command center only."""
    if current_user.role != "command_center":
        raise HTTPException(status_code=403, detail="Command center role required")

    report = db.query(Report).filter(Report.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Incident not found")

    if body.status not in WORKFLOW_STATUSES:
        raise HTTPException(status_code=400, detail=f"Status must be one of: {WORKFLOW_STATUSES}")

    _advance_report(report, body.status)
    db.commit()
    return _enrich_report(report, db)


@app.post("/incident/{report_id}/action")
def incident_action(
    report_id: int,
    body: ActionRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_role("command_center")),
):
    """Perform named action on incident (assign, in_progress, resolved)."""
    report = db.query(Report).filter(Report.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Incident not found")

    next_status = {
        "assign": "assigned",
        "in_progress": "in_progress",
        "resolved": "resolved",
    }[body.action]
    _advance_report(report, next_status, body.resource)

    db.commit()
    return _enrich_report(report, db)


# ---------------------------------------------------------------------------
# Command Center Stats (/command/stats)
# ---------------------------------------------------------------------------

@app.get("/command/stats")
def command_stats(
    db: Session = Depends(get_db),
    _: User = Depends(require_role("command_center")),
):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    all_reports = db.query(Report).all()

    active = sum(1 for r in all_reports if r.status not in ("resolved",))
    critical = sum(1 for r in all_reports if r.priority_score and r.priority_score >= 0.75)
    high = sum(1 for r in all_reports if r.priority_score and 0.50 <= r.priority_score < 0.75)
    people_affected = sum((r.people_affected or (r.severity or 1) * 20) for r in all_reports)
    resolved_today = sum(
        1 for r in all_reports
        if r.status == "resolved"
        and r.resolved_at
        and (
            r.resolved_at.replace(tzinfo=timezone.utc)
            if r.resolved_at.tzinfo is None
            else r.resolved_at
        ) >= today
    )

    return {
        "active_incidents": active,
        "critical_incidents": critical,
        "high_priority": high,
        "people_affected": people_affected,
        "resolved_today": resolved_today,
        "total": len(all_reports),
    }


# ---------------------------------------------------------------------------
# Telephony Root Endpoints (Twilio & Vobiz)
# ---------------------------------------------------------------------------

@app.post("/twilio/voice")
@app.post("/voice/twilio")
async def root_twilio_voice(request: Request, db: Session = Depends(get_db)):
    from routes_voice import handle_twilio_voice
    return await handle_twilio_voice(request, db)


@app.post("/voice/incoming")
@app.post("/twilio/voice-gather")
async def root_voice_incoming(request: Request, db: Session = Depends(get_db)):
    from routes_voice import voice_incoming_call
    return await voice_incoming_call(request, db)


@app.post("/voice/turn")
async def root_voice_turn(request: Request, db: Session = Depends(get_db)):
    from routes_voice import voice_conversation_turn
    return await voice_conversation_turn(request, db)


@app.post("/voice/outbound")
async def root_voice_outbound(request: Request, db: Session = Depends(get_db)):
    from routes_voice import outbound_voice_webhook
    return await outbound_voice_webhook(request, db)


@app.post("/twilio/status")
async def root_twilio_status(request: Request, db: Session = Depends(get_db)):
    from routes_voice import handle_telephony_hangup
    return await handle_telephony_hangup(request, db)


@app.post("/answer")
async def root_vobiz_answer(request: Request, db: Session = Depends(get_db)):
    from routes_voice import handle_telephony_answer
    return await handle_telephony_answer(request, db, provider="auto")


@app.post("/hangup")
async def root_vobiz_hangup(request: Request, db: Session = Depends(get_db)):
    from routes_voice import handle_telephony_hangup
    return await handle_telephony_hangup(request, db)


@app.post("/stream-status")
async def root_vobiz_stream_status(request: Request):
    from routes_voice import handle_vobiz_stream_status
    return await handle_vobiz_stream_status(request)


@app.websocket("/ws")
@app.websocket("/twilio/stream")
async def root_telephony_ws(websocket: WebSocket):
    from routes_voice import handle_telephony_websocket
    db = SessionLocal()
    try:
        await handle_telephony_websocket(websocket, db)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Serve Frontend Static Files
# ---------------------------------------------------------------------------

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
