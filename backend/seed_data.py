"""
FIRA – Seed Data Loader.
Reads seed_zones.json and seed_shelters.json from the data/ directory and
populates the SQLite database on first run.

Usage (standalone):
    python seed_data.py
"""

import json
import logging
import pathlib

from sqlalchemy.orm import Session

from database import Base, SessionLocal, engine
from models import Shelter, Zone

logger = logging.getLogger(__name__)

DATA_DIR = pathlib.Path(__file__).parent.parent / "data"


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

    if not zones_empty and not shelters_empty:
        logger.info("Seed check: tables already populated – skipping.")
        return

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

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to seed database.")
        raise


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
