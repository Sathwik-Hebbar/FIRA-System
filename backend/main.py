"""
FIRA – Flood Intelligence & Response Application
Main FastAPI entry point.

All routes are defined here so the `app` object exists before any router
is registered. Import order: app created → routers attached → lifespan runs.
"""

import os
import logging
import math
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Literal

from fastapi import FastAPI, APIRouter, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr
from dotenv import load_dotenv
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import Base, SessionLocal, engine, get_db
from seed_data import seed_if_empty
from models import User, Zone, Report, Shelter
from risk_engine import compute_risk
from priority_engine import rank_zones, compute_report_priority, priority_level

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JWT / Auth configuration
# ---------------------------------------------------------------------------
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
SECRET_KEY = os.getenv("JWT_SECRET")
if not SECRET_KEY:
    raise RuntimeError("JWT_SECRET must be set before starting FIRA")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "120"))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.query(User).filter(User.email == email).first()


def authenticate_user(db: Session, email: str, password: str) -> Optional[User]:
    user = get_user_by_email(db, email)
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "iat": datetime.utcnow()})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Could not validate credentials")
    email: str = payload.get("sub")
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid token payload")
    user = get_user_by_email(db, email)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def require_role(required_role: str):
    """Return a FastAPI dependency that enforces a specific role."""
    def role_checker(current_user: User = Depends(get_current_user)):
        if current_user.role != required_role:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"Role '{required_role}' required")
        return current_user
    return role_checker


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FIRA startup: initialising database…")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        seed_if_empty(db)
    finally:
        db.close()

    logger.info("FIRA startup complete.")
    yield
    logger.info("FIRA shutdown.")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="FIRA API",
    description="Flood Intelligence & Response Application backend.",
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

# ---------------------------------------------------------------------------
# Pydantic schemas
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


class ReportCreate(BaseModel):
    lat: float
    lng: float
    severity: int                         # 1–5
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
    created_at: datetime

    class Config:
        from_attributes = True


class StatusPatch(BaseModel):
    status: str


class ActionRequest(BaseModel):
    action: Literal["assign", "in_progress", "resolved"]
    resource: Optional[str] = None


WORKFLOW_STATUSES = ("reported", "prioritized", "assigned", "in_progress", "resolved")


def _advance_report(report: Report, next_status: str, resource: Optional[str] = None) -> None:
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


class SimulateRainRequest(BaseModel):
    zone_id: int
    delta: float


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------
@app.get("/")
def root():
    return {"message": "FIRA API is running.", "version": "1.0.0"}


# ---------------------------------------------------------------------------
# AUTH routes  /auth/login  /auth/me
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


@auth_router.get("/me", response_model=UserResponse)
def read_me(current_user: User = Depends(get_current_user)):
    return UserResponse.from_orm(current_user)


app.include_router(auth_router, prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# ZONE routes  /zones  /zones/{id}  /risk/{id}  /simulate-rain
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
        "id": z.id, "name": z.name, "lat": z.lat, "lng": z.lng,
        "rainfall_mm": z.rainfall_mm, "river_level_m": z.river_level_m,
        "danger_level_m": z.danger_level_m, "elevation_m": z.elevation_m,
        "drainage_quality": z.drainage_quality, "historical_frequency": z.historical_frequency,
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
    from risk_engine import bump_rainfall
    try:
        zone = bump_rainfall(db, req.zone_id, req.delta)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    risk = compute_risk(zone)
    return {"id": zone.id, "name": zone.name, "rainfall_mm": zone.rainfall_mm, "risk": risk}


# ---------------------------------------------------------------------------
# SHELTER routes  /shelters
# ---------------------------------------------------------------------------
@app.get("/shelters")
def list_shelters(db: Session = Depends(get_db)):
    shelters = db.query(Shelter).all()
    return [
        {"id": s.id, "name": s.name, "lat": s.lat, "lng": s.lng, "capacity": s.capacity}
        for s in shelters
    ]


# ---------------------------------------------------------------------------
# Helper: find nearest shelter
# ---------------------------------------------------------------------------
def _nearest_shelter(db: Session, lat: float, lng: float) -> Optional[Shelter]:
    shelters = db.query(Shelter).all()
    if not shelters:
        return None
    return min(
        shelters,
        key=lambda s: math.hypot(s.lat - lat, s.lng - lng),
    )


# ---------------------------------------------------------------------------
# Helper: enrich report with shelter name and priority level
# ---------------------------------------------------------------------------
def _enrich_report(report: Report, db: Session) -> dict:
    shelter_name = None
    if report.shelter_id:
        sh = db.query(Shelter).filter(Shelter.id == report.shelter_id).first()
        shelter_name = sh.name if sh else None
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
        "status": report.status,
        "shelter_id": report.shelter_id,
        "shelter_name": shelter_name,
        "assigned_resource": report.assigned_resource,
        "photo_url": report.photo_url,
        "created_at": report.created_at,
    }


# ---------------------------------------------------------------------------
# INCIDENT / REPORT routes (citizen submits, both roles read)
# ---------------------------------------------------------------------------

@app.post("/report")
def create_report(
    req: ReportCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Submit a flood incident report.
    Requires authentication. reported_by is set automatically from the JWT.
    """
    if current_user.role not in ("citizen", "command_center"):
        raise HTTPException(status_code=403, detail="Authentication required to submit reports")

    # Find the nearest zone to compute environmental risk
    zones = db.query(Zone).all()
    if zones:
        nearest_zone = min(zones, key=lambda z: math.hypot(z.lat - req.lat, z.lng - req.lng))
        env_risk = compute_risk(nearest_zone)["score"]
    else:
        env_risk = 0.5  # fallback if no zones seeded

    # Build report object
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
    db.flush()  # get ID without committing

    # Compute priority score using Priority Engine
    score = compute_report_priority(report, env_risk)
    report.priority_score = score
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
    List incidents.
    - Citizens see only their own reports.
    - Command center sees all, ordered by priority (highest first).
    """
    if current_user.role == "citizen":
        reports = (
            db.query(Report)
            .filter(Report.reported_by == current_user.id)
            .order_by(Report.created_at.desc())
            .all()
        )
    elif current_user.role == "command_center":
        # command_center: all incidents sorted by priority desc
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
    if current_user.role != "citizen" and current_user.role != "command_center":
        raise HTTPException(status_code=403, detail="Known application role required")

    return _enrich_report(report, db)


@app.patch("/incident/{report_id}")
def patch_incident_status(
    report_id: int,
    body: StatusPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update report status. Command center only (citizen gets 403)."""
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
    """
    Perform a named action on an incident. Command center only.
    Actions: assign, in_progress, resolved.
    """
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
# COMMAND CENTER stats  /command/stats
# ---------------------------------------------------------------------------
@app.get("/command/stats")
def command_stats(
    db: Session = Depends(get_db),
    _: User = Depends(require_role("command_center")),
):
    """KPI dashboard stats. Command center only."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    all_reports = db.query(Report).all()

    active = sum(1 for r in all_reports if r.status not in ("resolved",))
    critical = sum(1 for r in all_reports if r.priority_score and r.priority_score >= 0.75)
    high = sum(1 for r in all_reports if r.priority_score and 0.50 <= r.priority_score < 0.75)
    people_affected = sum((r.people_affected or r.severity * 20) for r in all_reports)
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
