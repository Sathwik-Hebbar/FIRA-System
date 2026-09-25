"""
FIRA – Seed Data Loader.
Reads seed_zones.json and seed_shelters.json from the data/ directory and
populates the SQLite database on first run. Also seeds two demo users.

Usage (standalone):
    python seed_data.py
"""

import json
import logging
import pathlib
from datetime import datetime

from passlib.context import CryptContext
from sqlalchemy.orm import Session

from database import Base, SessionLocal, engine
from models import Shelter, User, Zone

logger = logging.getLogger(__name__)

DATA_DIR = pathlib.Path(__file__).parent.parent / "data"

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _hash(password: str) -> str:
    return _pwd_context.hash(password)


# ---------------------------------------------------------------------------
# Public API – called from FastAPI startup
# ---------------------------------------------------------------------------

def seed_if_empty(db: Session) -> None:
    """
    Insert seed data only when both the Zone and Shelter tables are empty.

    This is idempotent: calling it on an already-populated database is a no-op,
    so it is safe to invoke on every application startup.

    Parameters
    ----------
    db : Session
        An active SQLAlchemy session (injected by the caller).
    """
    zones_empty = db.query(Zone).first() is None
    shelters_empty = db.query(Shelter).first() is None

    try:
        if zones_empty:
            zones_path = DATA_DIR / "seed_zones.json"
            with open(zones_path, encoding="utf-8") as f:
                zones = json.load(f)
            db.bulk_insert_mappings(Zone, zones)
            logger.info("Seeded %d flood zones.", len(zones))

        if shelters_empty:
            shelters_path = DATA_DIR / "seed_shelters.json"
            with open(shelters_path, encoding="utf-8") as f:
                shelters = json.load(f)
            db.bulk_insert_mappings(Shelter, shelters)
            logger.info("Seeded %d shelters.", len(shelters))

        if zones_empty or shelters_empty:
            db.commit()

        # Seed demo users (always check – idempotent)
        _seed_demo_users(db)

    except Exception:
        db.rollback()
        logger.exception("Failed to seed database.")
        raise


def _seed_demo_users(db: Session) -> None:
    """Create demo citizen and command_center accounts if they don't exist."""
    demo_users = [
        {
            "name": "Demo Citizen",
            "email": "citizen@floodguard.demo",
            "password": "Demo@123",
            "role": "citizen",
            "phone": "555-0001",
            "latitude": 28.6750,
            "longitude": 77.1000,
        },
        {
            "name": "Command Center",
            "email": "command@floodguard.demo",
            "password": "Demo@123",
            "role": "command_center",
            "phone": "555-0002",
            "latitude": None,
            "longitude": None,
        },
    ]

    created = 0
    for data in demo_users:
        if not db.query(User).filter(User.email == data["email"]).first():
            user = User(
                name=data["name"],
                email=data["email"],
                password_hash=_hash(data["password"]),
                role=data["role"],
                phone=data["phone"],
                latitude=data["latitude"],
                longitude=data["longitude"],
            )
            db.add(user)
            created += 1

    if created:
        db.commit()
        logger.info("Seeded %d demo user(s).", created)
    else:
        logger.info("Demo users already present – skipping.")


# ---------------------------------------------------------------------------
# Standalone helper – drop-and-recreate (dev / CI use only)
# ---------------------------------------------------------------------------

def seed():
    """Drop-and-recreate all tables, then insert seed data unconditionally."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        seed_if_empty(db)
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed()
