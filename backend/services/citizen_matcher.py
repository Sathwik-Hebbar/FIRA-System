"""
FIRA – Citizen Matcher Service (Phase 2).
Matches incoming caller phone numbers against the existing FIRA User database.
Deterministic and secure; never uses an LLM for identity lookup.
"""

import re
from typing import Optional, Tuple
from sqlalchemy.orm import Session

import sys
from pathlib import Path
_backend_dir = str(Path(__file__).resolve().parent.parent)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from models import User
from schemas.voice_sos import VoiceCitizenProfile


def normalize_phone_number(raw_phone: str) -> str:
    """
    Normalizes a phone number to standard digits.
    Handles +91, leading 0, spaces, dashes, and parentheses.
    Returns standard 10-digit number if Indian mobile, or digits string.
    """
    if not raw_phone:
        return ""
    # Extract only digits
    digits = re.sub(r"\D", "", raw_phone)
    # Strip leading country code 91 if 12 digits
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    # Strip leading 0 if 11 digits
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    return digits


def match_citizen_by_phone(
    db: Session,
    raw_phone: str,
    language: str = "kn-IN"
) -> Tuple[VoiceCitizenProfile, bool]:
    """
    Matches caller phone number against existing User database.

    Parameters
    ----------
    db : Session
        Active database session.
    raw_phone : str
        Incoming caller phone number.
    language : str
        Preferred conversation language.

    Returns
    -------
    (VoiceCitizenProfile, bool)
        Tuple of (VoiceCitizenProfile, is_verified_citizen)
    """
    normalized_digits = normalize_phone_number(raw_phone)
    if not normalized_digits:
        # Unknown or hidden caller ID
        return VoiceCitizenProfile(
            id=None,
            name="Anonymous Caller",
            phone=raw_phone or "UNKNOWN",
            language=language,
            is_verified=False,
        ), False

    # 1. Exact match on stored phone
    user = db.query(User).filter(User.phone == raw_phone).first()

    # 2. Match on normalized phone if raw did not match
    if not user:
        all_users = db.query(User).filter(User.phone.isnot(None)).all()
        for u in all_users:
            if u.phone and normalize_phone_number(u.phone) == normalized_digits:
                user = u
                break

    # Verified citizen found in DB
    if user:
        return VoiceCitizenProfile(
            id=user.id,
            name=user.name,
            phone=user.phone or raw_phone,
            language=language,
            latitude=user.latitude,
            longitude=user.longitude,
            is_verified=True,
        ), True

    # Unverified / unregistered caller: do NOT invent permanent citizen
    return VoiceCitizenProfile(
        id=None,
        name=f"Caller ({raw_phone})",
        phone=raw_phone,
        language=language,
        latitude=None,
        longitude=None,
        is_verified=False,
    ), False
