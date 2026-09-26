"""
FIRA – Database configuration.
Sets up SQLAlchemy engine and session factory targeting a local SQLite file.
"""

from pathlib import Path
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DB_PATH = Path(__file__).resolve().parent.parent / "fira.db"
# Deployment can replace SQLite without changing application code. SQLite
# remains the safe zero-config MVP default.
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_schema_migrations():
    """Ensure SQLite schema has newly added columns without requiring manual migrations."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            table_check = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='reports'")
            ).fetchone()
            if table_check:
                info = conn.execute(text("PRAGMA table_info(reports)")).fetchall()
                cols = [row[1] for row in info]
                if "source" not in cols:
                    conn.execute(text("ALTER TABLE reports ADD COLUMN source VARCHAR DEFAULT 'WEB'"))
                additions = {
                    "voice_session_id": "VARCHAR",
                    "raw_input": "VARCHAR",
                    "normalized_data": "VARCHAR",
                    "risk_score": "FLOAT",
                    "risk_level": "VARCHAR",
                    "priority_reasons": "VARCHAR",
                    "ai_analysis": "VARCHAR",
                    "updated_at": "DATETIME",
                }
                for name, column_type in additions.items():
                    if name not in cols:
                        conn.execute(text(f"ALTER TABLE reports ADD COLUMN {name} {column_type}"))
                voice_check = conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name='voice_sessions'")
                ).fetchone()
                if voice_check:
                    voice_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(voice_sessions)")).fetchall()]
                    if "raw_payload" not in voice_cols:
                        conn.execute(text("ALTER TABLE voice_sessions ADD COLUMN raw_payload VARCHAR"))
                conn.commit()
    except Exception:
        pass
