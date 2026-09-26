"""
FIRA – SQLite to Neon PostgreSQL Migration Tool.

This script migrates data from your local SQLite database (fira.db)
to a remote Neon Serverless PostgreSQL database.

Usage:
    python migrate_to_neon.py [NEON_DATABASE_URL]

Or set DATABASE_URL in .env and run:
    python migrate_to_neon.py
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Add backend to sys.path
backend_path = Path(__file__).resolve().parent / "backend"
sys.path.insert(0, str(backend_path))

from models import Base, User, Shelter, Zone, VoiceSession, Report


def get_neon_url() -> str:
    """Retrieve Neon database URL from CLI args or environment."""
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip().strip("\"'")

    neon_url = (os.getenv("NEON_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip("\"'")
    if neon_url and not neon_url.startswith("sqlite"):
        return neon_url

    print("=" * 60)
    print(" FIRA -> NEON DATABASE MIGRATION")
    print("=" * 60)
    print("\nPlease enter your Neon PostgreSQL Connection String.")
    print("Example: postgresql://neondb_owner:npg_xyz@ep-quiet-1234.us-east-2.aws.neon.tech/neondb?sslmode=require\n")
    entered = input("Neon Database URL: ").strip().strip("\"'")
    return entered


def normalize_postgres_url(url: str) -> str:
    """Ensure standard postgresql:// prefix for SQLAlchemy."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def migrate():
    neon_raw_url = get_neon_url()
    if not neon_raw_url:
        print("[-] Error: No Neon Database URL provided. Migration aborted.")
        sys.exit(1)

    neon_url = normalize_postgres_url(neon_raw_url)

    if not (neon_url.startswith("postgresql://") or neon_url.startswith("postgresql+")):
        print(f"[-] Error: URL does not look like a PostgreSQL connection string: {neon_url}")
        print("    Neon connection strings should start with postgresql://")
        sys.exit(1)

    sqlite_path = Path(__file__).resolve().parent / "fira.db"
    if not sqlite_path.exists():
        print(f"[-] Warning: Local SQLite database file not found at {sqlite_path}")
        print("    Creating schema in Neon from SQLAlchemy models...")
        sqlite_engine = None
    else:
        sqlite_engine = create_engine(f"sqlite:///{sqlite_path}")
        print(f"[+] Found source SQLite database: {sqlite_path}")

    print("[+] Connecting to Neon PostgreSQL...")
    try:
        neon_engine = create_engine(
            neon_url,
            pool_pre_ping=True,
            pool_recycle=300,
        )
        with neon_engine.connect() as conn:
            version = conn.execute(text("SELECT version();")).fetchone()[0]
            print(f"[+] Successfully connected to Neon!")
            print(f"    PostgreSQL version: {version.split(',')[0]}")
    except Exception as e:
        print(f"[-] Failed to connect to Neon PostgreSQL: {e}")
        sys.exit(1)

    # 1. Create all tables in Neon
    print("\n[+] Creating database tables in Neon (if not present)...")
    Base.metadata.create_all(bind=neon_engine)
    print("[+] Tables verified/created successfully.")

    if not sqlite_engine:
        print("\n[+] No source SQLite data to migrate. Initial setup is complete!")
        return

    # 2. Migrate data
    SqliteSession = sessionmaker(bind=sqlite_engine)
    NeonSession = sessionmaker(bind=neon_engine)

    sqlite_db = SqliteSession()
    neon_db = NeonSession()

    print("\n" + "=" * 60)
    print(" Transferring Records from SQLite to Neon")
    print("=" * 60)

    try:
        # Step A: Users
        users = sqlite_db.query(User).all()
        for u in users:
            neon_db.merge(u)
        neon_db.commit()
        print(f"[+] users           : Migrated/Synced {len(users)} records.")

        # Step B: Shelters
        shelters = sqlite_db.query(Shelter).all()
        for s in shelters:
            neon_db.merge(s)
        neon_db.commit()
        print(f"[+] shelters        : Migrated/Synced {len(shelters)} records.")

        # Step C: Zones
        zones = sqlite_db.query(Zone).all()
        for z in zones:
            neon_db.merge(z)
        neon_db.commit()
        print(f"[+] zones           : Migrated/Synced {len(zones)} records.")

        # Step D: VoiceSessions (Temporarily decouple incident_id to resolve circular FK with reports)
        voice_sessions = sqlite_db.query(VoiceSession).all()
        vses_incident_map = {}
        for vs in voice_sessions:
            vses_incident_map[vs.id] = vs.incident_id
            # Temporarily clear incident_id so it doesn't violate foreign key to reports
            vs.incident_id = None
            neon_db.merge(vs)
        neon_db.commit()
        print(f"[+] voice_sessions  : Migrated/Synced {len(voice_sessions)} records (Pass 1).")

        # Step E: Reports
        reports = sqlite_db.query(Report).all()
        for r in reports:
            neon_db.merge(r)
        neon_db.commit()
        print(f"[+] reports         : Migrated/Synced {len(reports)} records.")

        # Step F: Restore incident_id links in VoiceSessions
        for vs_id, inc_id in vses_incident_map.items():
            if inc_id is not None:
                neon_db.query(VoiceSession).filter(VoiceSession.id == vs_id).update({"incident_id": inc_id})
        neon_db.commit()
        print(f"[+] voice_sessions  : Re-linked incident foreign keys (Pass 2).")

        # Step G: Reset PostgreSQL sequences for tables with auto-increment primary keys
        for table_name, id_col in [("users", "id"), ("shelters", "id"), ("zones", "id"), ("reports", "id")]:
            try:
                with neon_engine.connect() as conn:
                    seq_sql = f"""
                    SELECT setval(
                        pg_get_serial_sequence('{table_name}', '{id_col}'),
                        COALESCE(MAX({id_col}), 1),
                        MAX({id_col}) IS NOT NULL
                    ) FROM {table_name};
                    """
                    conn.execute(text(seq_sql))
                    conn.commit()
            except Exception:
                pass

        print("\n" + "=" * 60)
        print(" Verification: Row Counts in Neon PostgreSQL")
        print("=" * 60)
        for model_cls, table_name in [
            (User, "users"),
            (Shelter, "shelters"),
            (Zone, "zones"),
            (VoiceSession, "voice_sessions"),
            (Report, "reports"),
        ]:
            count = neon_db.query(model_cls).count()
            print(f"  - {table_name:16}: {count} rows")

        print("\n[SUCCESS] Migration completed successfully!")

    except Exception as e:
        neon_db.rollback()
        print(f"\n[-] Error during migration: {e}")
        import traceback
        traceback.print_exc()
    finally:
        sqlite_db.close()
        neon_db.close()


if __name__ == "__main__":
    migrate()
