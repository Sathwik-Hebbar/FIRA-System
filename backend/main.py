"""
FIRA – Flood Intelligence & Response Application
Main FastAPI entry point.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from database import Base, SessionLocal, engine
from seed_data import seed_if_empty

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.

    Startup:
      - Ensures all tables exist (create_all is a no-op if they do).
      - Seeds Zone and Shelter tables if they are empty.
    Shutdown:
      - (placeholder for cleanup logic)
    """
    # --- Startup ---
    logger.info("FIRA startup: initialising database…")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        seed_if_empty(db)
    finally:
        db.close()

    logger.info("FIRA startup complete.")

    yield  # Application runs here

    # --- Shutdown ---
    logger.info("FIRA shutdown.")


app = FastAPI(
    title="FIRA API",
    description="Flood Intelligence & Response Application backend.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/")
def root():
    return {"message": "FIRA API is running."}
