"""
FIRA – Seed Data Loader.
Reads seed_zones.json and seed_shelters.json from the data/ directory and
populates the Neon PostgreSQL database on first run. Also seeds demo users.

Usage (standalone):
    python seed_data.py
"""

import json
import logging
import os
import pathlib
import sys
from datetime import datetime, timezone

# Ensure backend directory is in sys.path
_backend_dir = str(pathlib.Path(__file__).resolve().parent)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

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
    Insert seed data only when tables are empty.

    This is idempotent: calling it on an already-populated database is safe.
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
    """Create default citizen and command_center accounts if they don't exist."""
    default_users = [
        {
            "name": "Resident Citizen",
            "email": "citizen@floodguard.org",
            "password": "Password@123",
            "role": "citizen",
            "phone": "555-0001",
            "latitude": 28.6750,
            "longitude": 77.1000,
        },
        {
            "name": "Command Center",
            "email": "command@floodguard.org",
            "password": "Password@123",
            "role": "command_center",
            "phone": "555-0002",
            "latitude": None,
            "longitude": None,
        },
        {
            "name": "Ramesh Gowda",
            "email": "ramesh@fira.org",
            "password": "Password@123",
            "role": "citizen",
            "phone": "+91 98765 43210",
            "latitude": 12.9716,
            "longitude": 77.5946,
        },
    ]

    created = 0
    for data in default_users:
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
        logger.info("Seeded %d user account(s).", created)
    else:
        logger.info("Users already present – skipping.")


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
