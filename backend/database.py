"""
FIRA – Database configuration.
Configured for Neon Serverless PostgreSQL using SQLAlchemy.
"""

import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

# Read Neon PostgreSQL connection string from environment
DATABASE_URL = os.getenv("DATABASE_URL", "").strip("\"'")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Please ensure DATABASE_URL pointing to Neon PostgreSQL "
        "is defined in your .env file."
    )

# Normalize postgres:// to postgresql:// for SQLAlchemy compatibility
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Neon Serverless PostgreSQL engine configuration
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,  # Automatically tests connection validity and reconnects on idle disconnects
    pool_recycle=300,    # Recycles connections every 5 minutes
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a database session connected to Neon PostgreSQL."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_schema_migrations():
    """Schema migrations are managed directly on Neon PostgreSQL via SQLAlchemy models."""
    pass
