"""
FIRA – Voice SOS API Routes.
Implements the Multilingual AI Emergency Voice Agent endpoints:
- POST /api/voice-sos/process (Direct audio/transcript processing pipeline)
- POST /api/voice/incoming (Telephony webhook for incoming calls)
- POST /api/voice/turn (Multi-turn conversational flow)
- POST /api/voice/finalize (Finalize voice call and commit emergency report)
- GET /api/voice/sessions (List voice sessions for Command Center)
- GET /api/voice/sessions/{session_id} (Detailed voice session inspection)
- WebSocket /api/voice/stream (Real-time telephony media streaming)
"""

import asyncio
import base64
import html
import io
import json
import logging
import math
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import re
import uuid
import wave

import httpx

try:
    import audioop
except ImportError:
    audioop = None


from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import SessionLocal, get_db
from models import Report, Shelter, User, VoiceSession, Zone
from priority_engine import compute_priority, compute_report_priority, priority_level
from risk_engine import compute_risk
from schemas.voice_sos import (
    EmergencyExtraction,
    NormalizedFIRASOS,
    VoiceCitizenProfile,
    VoiceProcessRequest,
    VoiceProcessResponse,
    VoiceSessionDetail,
)
from services.citizen_matcher import match_citizen_by_phone, normalize_phone_number
from services.conversation_manager import (
    CONFIRMATION_MESSAGES,
    SAFETY_INSTRUCTIONS,
    VoiceSessionState,
    get_or_create_session,
)
from services.gemini_extractor import gemini_extractor
from services.sarvam_stt import sarvam_stt
from services.sarvam_tts import STANDARD_CONFIRMATIONS, sarvam_tts
from services.sos_merger import calculate_derived_severity, merge_voice_sos
from services.incident_processor import process_incident

logger = logging.getLogger("fira.voice_sos")

router = APIRouter(prefix="/api", tags=["Voice SOS"])


class SarvamVoiceSOSRequest(VoiceProcessRequest):
    """Call context sent by the Sarvam Voice Agent API tool after a call."""
    session_id: str


@router.post("/sarvam/voice-sos")
@router.post("/voice-sos/sarvam")
async def sarvam_voice_sos(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Secure Sarvam adapter supporting both:
    1. Sarvam Voice Agent API Tool (invoked on_end with Bearer token)
    2. Sarvam Platform Post-Call Webhook (invoked with call transcript & metadata)
    """
    # 1. Parse JSON body, form data, query parameters, and custom headers safely
    payload: Dict[str, Any] = {}
    try:
        payload = await request.json()
    except Exception:
        try:
            form = await request.form()
            payload = dict(form)
        except Exception:
            payload = {}

    if not isinstance(payload, dict):
        payload = {}

    # Merge query parameters if present
    if request.query_params:
        payload = {**dict(request.query_params), **payload}

    # Inspect headers for custom fields
    for h_key, h_val in request.headers.items():
        h_lower = h_key.lower()
        if h_lower in ["user_name", "username", "caller_phone", "phone"] and "caller_phone" not in payload:
            payload["caller_phone"] = h_val
        elif h_lower in ["call_summary", "transcript", "summary"] and "transcript" not in payload:
            payload["transcript"] = h_val

    # Unpack nested "Output" if present (Sarvam API tool payload format)
    output_val = payload.get("Output") or payload.get("output")
    if isinstance(output_val, str):
        try:
            output_val = json.loads(output_val)
        except Exception:
            pass
    if isinstance(output_val, dict):
        for k, v in output_val.items():
            if k not in payload or not payload[k] or str(payload[k]).startswith("@"):
                payload[k] = v

    # Unpack "final_agent_variables" if present (Sarvam post-call webhook format)
    fav = payload.get("final_agent_variables")
    if isinstance(fav, dict):
        for k, v in fav.items():
            if k in ("Output", "output"):
                nested = v
                if isinstance(nested, str):
                    try:
                        nested = json.loads(nested)
                    except Exception:
                        pass
                if isinstance(nested, dict):
                    for nk, nv in nested.items():
                        if nk not in payload or not payload[nk] or str(payload[nk]).startswith("@"):
                            payload[nk] = nv
            elif k not in payload or not payload[k] or str(payload[k]).startswith("@"):
                payload[k] = v

    # 2. Authentication check
    expected_token = os.getenv("SARVAM_FIRA_WEBHOOK_TOKEN", "").strip()
    helpline_number = os.getenv("FIRA_HELPLINE_NUMBER", "+918071579681").strip()
    voice_agent_id = os.getenv("VOICE_AGENT_ID", "").strip()

    is_authenticated = False
    authorization = request.headers.get("authorization", "")
    if authorization:
        scheme, _, supplied_token = authorization.partition(" ")
        if scheme.lower() == "bearer" and expected_token and secrets.compare_digest(supplied_token, expected_token):
            is_authenticated = True

    # Check query param token (?token=... or ?secret=...) or custom header
    query_token = request.query_params.get("token") or request.query_params.get("secret") or request.query_params.get("key")
    header_token = request.headers.get("x-sarvam-token") or request.headers.get("x-webhook-token")
    if (query_token and expected_token and secrets.compare_digest(query_token, expected_token)) or \
       (header_token and expected_token and secrets.compare_digest(header_token, expected_token)):
        is_authenticated = True

    # Check verified Sarvam platform post-call webhook matching our helpline / agent deployment
    agent_phone = str(payload.get("agent_phone_number") or "").strip()
    app_id = str(payload.get("app_id") or "").strip()
    deployment_id = str(payload.get("deployment_id") or "").strip()

    if not is_authenticated:
        if (helpline_number and agent_phone == helpline_number) or \
           (app_id and (app_id.startswith("FIRA") or (voice_agent_id and app_id == voice_agent_id))) or \
           (deployment_id and deployment_id.startswith("FIRA")):
            logger.info(
                "Authenticated Sarvam post-call webhook via verified agent metadata (app_id=%s, agent_phone=%s)",
                app_id, agent_phone
            )
            is_authenticated = True

    if not is_authenticated:
        logger.warning("Unauthorized Sarvam webhook call. Auth header: %s, params: %s", authorization, dict(request.query_params))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid integration credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    logger.info("Received authenticated Sarvam SOS payload: keys=%s", list(payload.keys()))

    # 3. Extract caller_phone (filter out unreplaced '@' placeholder variables)
    phone_candidates = [
        payload.get("user_phone_number"),
        payload.get("caller_phone"),
        payload.get("phone"),
        payload.get("user_identifier"),
        payload.get("User Identifier"),
        payload.get("from"),
        payload.get("caller"),
        payload.get("user_name"),
    ]
    caller_phone = None
    for p in phone_candidates:
        if p and str(p).strip() and not str(p).strip().startswith("@"):
            caller_phone = str(p).strip()
            break
    if not caller_phone:
        caller_phone = "+91-SARVAM-CALLER"

    # 4. Extract session_id (filter out unreplaced '@' placeholder variables)
    sid_candidates = [
        payload.get("interaction_id"),
        payload.get("session_id"),
        payload.get("Interaction ID"),
        payload.get("call_id"),
        payload.get("id"),
    ]
    session_id = None
    for s in sid_candidates:
        if s and str(s).strip() and not str(s).strip().startswith("@"):
            session_id = str(s).strip()
            break
    if not session_id:
        session_id = f"sarvam_{uuid.uuid4().hex[:12]}"

    # 5. Extract language
    raw_lang = payload.get("language") or payload.get("lang") or os.getenv("AGENT_LANGUAGE", "kn-IN")
    language = str(raw_lang).strip()
    if language.startswith("@") or not language:
        language = os.getenv("AGENT_LANGUAGE", "kn-IN")

    # 6. Extract transcript from interaction_transcript (turns) or text fields
    interaction_turns = payload.get("interaction_transcript")
    dialogue_lines: List[str] = []
    user_speech_indic: List[str] = []
    user_speech_en: List[str] = []
    structured_history: List[Dict[str, Any]] = []

    if isinstance(interaction_turns, list):
        for turn in interaction_turns:
            if not isinstance(turn, dict):
                continue
            role = turn.get("role", "caller")
            role_label = "Agent" if role == "agent" else "Caller"
            indic_text = (turn.get("indic_text") or "").strip()
            en_text = (turn.get("en_text") or "").strip()
            text = indic_text or en_text
            if text:
                dialogue_lines.append(f"{role_label}: {text}")
                structured_history.append({"role": role, "text": text, "en": en_text, "indic": indic_text})
            if role == "user":
                if indic_text:
                    user_speech_indic.append(indic_text)
                if en_text:
                    user_speech_en.append(en_text)

    transcript_candidates = [
        payload.get("transcript"),
        payload.get("call_summary"),
        payload.get("summary"),
        payload.get("text"),
        payload.get("Call Transcript"),
        payload.get("Call Summary"),
        payload.get("description"),
        payload.get("message"),
    ]
    transcript = None
    for t in transcript_candidates:
        if t and str(t).strip() and not str(t).strip().startswith("@"):
            transcript = str(t).strip()
            break

    if not transcript:
        if user_speech_indic:
            transcript = " ".join(user_speech_indic)
        elif user_speech_en:
            transcript = " ".join(user_speech_en)
        elif dialogue_lines:
            transcript = "\n".join(dialogue_lines)
        else:
            transcript = "Emergency Voice Call received via Sarvam AI Voice Agent (call completed)"

    # 7. Check for duplicate delivery (Sarvam retries failed on_end tool deliveries)
    prior_session = db.query(VoiceSession).filter(VoiceSession.id == session_id).first()
    if prior_session and prior_session.incident_id:
        prior_report = db.query(Report).filter(Report.id == prior_session.incident_id).first()
        if prior_report:
            logger.info("Duplicate Sarvam delivery for session %s - returning existing report %s", session_id, prior_report.id)
            return {
                "success": True,
                "sos_id": prior_report.id,
                "session_id": prior_session.id,
                "source": "VOICE_CALL",
                "status": prior_report.status,
                "duplicate_delivery": True,
            }

    # 8. Extract optional coordinates
    lat_val = None
    lng_val = None
    try:
        if payload.get("lat") is not None and not str(payload["lat"]).startswith("@"):
            lat_val = float(payload["lat"])
        if payload.get("lng") is not None and not str(payload["lng"]).startswith("@"):
            lng_val = float(payload["lng"])
    except (ValueError, TypeError):
        pass

    voice_req = VoiceProcessRequest(
        caller_phone=caller_phone,
        transcript=transcript,
        session_id=session_id,
        language=language,
        lat=lat_val,
        lng=lng_val,
        conversation_history=structured_history or None,
    )

    result = await process_voice_sos(voice_req, db)
    logger.info("Successfully processed Sarvam SOS: report_id=%s, session_id=%s", result.sos_id, result.session_id)
    return result


# ---------------------------------------------------------------------------
# Helper: Find nearest zone risk
# ---------------------------------------------------------------------------

def _get_zone_risk(db: Session, lat: Optional[float], lng: Optional[float]) -> float:
    """Calculates risk score (0.0 to 1.0) of the zone nearest to the coordinates."""
    if lat is None or lng is None:
        return 0.5

    zones = db.query(Zone).all()
    if not zones:
        return 0.5

    nearest = min(zones, key=lambda z: math.hypot(z.lat - lat, z.lng - lng))
    try:
        risk_data = compute_risk(nearest)
        return float(risk_data.get("score", 0.5))
    except Exception:
        return 0.5


def _get_nearest_shelter_id(db: Session, lat: Optional[float], lng: Optional[float]) -> Optional[int]:
    """Finds nearest shelter ID if coordinates are available."""
    if lat is None or lng is None:
        return None
    shelters = db.query(Shelter).all()
    if not shelters:
        return None
    nearest = min(shelters, key=lambda s: math.hypot(s.lat - lat, s.lng - lng))
    return nearest.id


# ---------------------------------------------------------------------------
# 1. Main Pipeline: POST /api/voice-sos/process
# ---------------------------------------------------------------------------

@router.post("/voice-sos/process", response_model=VoiceProcessResponse)
async def process_voice_sos(
    req: VoiceProcessRequest,
    db: Session = Depends(get_db),
):
    """
    Complete end-to-end Voice SOS pipeline:
    Speech/Transcript -> Translation -> Extraction -> Citizen Match -> Merge
    -> Existing Priority Engine -> Neon Database -> Kannada TTS Confirmation.
    """
    session_id = req.session_id or f"vses_{uuid.uuid4().hex[:12]}"
    caller_phone = req.caller_phone
    target_lang = req.language or "kn-IN"
    conversation_history = getattr(req, "conversation_history", None)

    raw_transcript: Optional[str] = req.transcript
    translated_transcript: Optional[str] = None

    # Step 1: Speech-to-Text & Translation (if audio is provided)
    if req.audio_base64:
        try:
            audio_bytes = base64.b64decode(req.audio_base64)
            stt_result = sarvam_stt.transcribe_and_translate(
                audio_bytes=audio_bytes,
                language_code=target_lang,
            )
            raw_transcript = stt_result.get("raw_transcript") or raw_transcript
            translated_transcript = stt_result.get("translated_transcript")
        except Exception as exc:
            logger.error("STT processing error in process_voice_sos: %s", exc)

    # Translate non-English transcript if not already translated
    if raw_transcript and not translated_transcript and not target_lang.startswith("en"):
        try:
            trans_res = sarvam_stt.translate_text(raw_transcript, source_lang=target_lang)
            translated_transcript = trans_res.get("translated_text")
        except Exception as exc:
            logger.warning("Translation error in process_voice_sos: %s", exc)

    # Step 2: Information Extraction via Gemini (strictly factual, structured JSON)
    text_for_extraction = translated_transcript or raw_transcript or ""
    if not text_for_extraction.strip():
        # No speech content provided
        extraction = EmergencyExtraction()
    else:
        extraction = gemini_extractor.extract_emergency_facts(
            transcript=text_for_extraction,
            original_transcript=raw_transcript,
        )

    # Step 3: Citizen Matching against FIRA User database (Deterministic by phone)
    citizen_profile, is_matched = match_citizen_by_phone(db, caller_phone, target_lang)

    # Step 4: SOS Merge (Authoritative DB profile + AI facts)
    normalized_sos = merge_voice_sos(
        citizen=citizen_profile,
        extraction=extraction,
        session_id=session_id,
        transcript=raw_transcript,
        translated_transcript=translated_transcript,
        language=target_lang,
        telephony_lat=req.lat,
        telephony_lng=req.lng,
    )

    # Step 5: Deterministic Triage Severity & Existing Priority Engine
    derived_severity = calculate_derived_severity(extraction)
    lat = normalized_sos.location.get("lat")
    lng = normalized_sos.location.get("lng")
    env_risk = _get_zone_risk(db, lat, lng)

    # Synthesize description for Priority Engine keyword and vulnerability matching
    desc_parts = [
        raw_transcript or "",
        f"[Location: {extraction.location_description}]" if extraction.location_description else "",
        f"[Notes: {extraction.additional_information}]" if extraction.additional_information else "",
    ]
    if extraction.trapped:
        desc_parts.append("trapped unable to leave cannot get out")
    if extraction.injured:
        desc_parts.append("injured medical emergency")
    if extraction.children_count and extraction.children_count > 0:
        desc_parts.append(f"{extraction.children_count} children present")
    if extraction.elderly_count and extraction.elderly_count > 0:
        desc_parts.append(f"{extraction.elderly_count} elderly present")

    full_description = " ".join(p for p in desc_parts if p).strip()

    SEVERITY_MAP = {1: "Low", 2: "Low", 3: "Medium", 4: "High", 5: "Critical"}
    sev_str = SEVERITY_MAP.get(derived_severity, "Medium")

    # Call EXISTING Priority Engine (single source of truth for priority calculation)
    priority_res = compute_priority(
        severity=sev_str,
        description=full_description,
        zone_risk_score=env_risk,
        lat=lat,
        lng=lng,
    )
    normalized_priority_score = round(priority_res["score"] / 100.0, 3)

    # Step 6: Database Persistence
    # CRITICAL: In PostgreSQL / Neon, reports.voice_session_id has a foreign key constraint
    # referencing voice_sessions.id. Therefore, the VoiceSession row MUST be added and flushed
    # in the database BEFORE adding/flushing incident_report!
    voice_session_record = db.query(VoiceSession).filter(VoiceSession.id == session_id).first()
    conv_hist = conversation_history or [{"role": "caller", "text": raw_transcript or ""}]
    if not voice_session_record:
        voice_session_record = VoiceSession(
            id=session_id,
            citizen_id=citizen_profile.id,
            caller_phone=caller_phone,
            language=target_lang,
            started_at=datetime.now(timezone.utc),
            ended_at=datetime.now(timezone.utc),
            transcript=raw_transcript,
            translated_transcript=translated_transcript,
            extracted_data=json.dumps(extraction.model_dump()),
            raw_payload=json.dumps({"transcript": raw_transcript, "translated_transcript": translated_transcript}),
            conversation_history=json.dumps(conv_hist),
            status="PROCESSING",
            incident_id=None,
        )
        db.add(voice_session_record)
        db.flush()
    else:
        if raw_transcript:
            voice_session_record.transcript = raw_transcript
        if translated_transcript:
            voice_session_record.translated_transcript = translated_transcript
        voice_session_record.extracted_data = json.dumps(extraction.model_dump())
        if conversation_history:
            voice_session_record.conversation_history = json.dumps(conversation_history)
        voice_session_record.ended_at = datetime.now(timezone.utc)
        db.flush()

    nearest_shelter_id = _get_nearest_shelter_id(db, lat, lng)
    shelter_obj = db.query(Shelter).filter(Shelter.id == nearest_shelter_id).first() if nearest_shelter_id else None

    # Create Report (Foreign key voice_session_id=session_id now exists in voice_sessions!)
    incident_report = Report(
        name=citizen_profile.name or f"Voice Caller ({caller_phone})",
        phone=citizen_profile.phone,
        reported_by=citizen_profile.id,
        lat=lat if lat is not None else 12.9716,
        lng=lng if lng is not None else 77.5946,
        severity=derived_severity,
        description=f"[VOICE SOS - {target_lang}] {raw_transcript or 'Audio Call'}\n(Translation: {translated_transcript or 'N/A'})\nDetails: {extraction.additional_information or 'None'}",
        flood_type="FLOOD",
        water_depth_m=extraction.water_depth_m,
        people_affected=extraction.people_count or 1,
        people_trapped=extraction.people_count if extraction.trapped else (1 if extraction.trapped else 0),
        medical_emergency=bool(extraction.medical_emergency or extraction.injured),
        priority_score=normalized_priority_score,
        status="prioritized",
        shelter_id=nearest_shelter_id,
        source="VOICE_CALL",
        voice_session_id=session_id,
    )
    db.add(incident_report)
    db.flush()

    # Canonical pipeline is the source of truth: preserve raw input, normalize
    # once, calculate deterministic risk/priority, and persist all outputs.
    incident_report, canonical_incident = process_incident(
        db,
        source="VOICE_CALL",
        raw_input={
            "transcript": raw_transcript,
            "translated_transcript": translated_transcript,
            "extraction": extraction.model_dump(),
            "session_id": session_id,
        },
        extraction=extraction,
        citizen=citizen_profile,
        location=normalized_sos.location,
        session_id=session_id,
        location_risk=env_risk,
        report=incident_report,
    )

    canonical_priority = canonical_incident["priority"]
    canonical_level_str = str(canonical_priority.get("level", "")).upper()
    mapped_level = "Critical" if "CRITICAL" in canonical_level_str else "High" if "HIGH" in canonical_level_str else "Medium" if "MEDIUM" in canonical_level_str else "Low"

    priority_res = {
        **priority_res,
        "score": canonical_priority["score"],
        "level": mapped_level,
        "reason": "; ".join(canonical_priority["reason"]),
    }
    normalized_priority_score = incident_report.priority_score

    # Link VoiceSession to Report and finalize
    voice_session_record.incident_id = incident_report.id
    voice_session_record.status = "SOS_CREATED"
    db.commit()
    db.refresh(incident_report)

    # Standardized Incident Contract
    prio_level_str = canonical_priority["level"]
    prio_level_upper = "CRITICAL" if prio_level_str.startswith("P1") else "HIGH" if prio_level_str.startswith("P2") else "MEDIUM" if prio_level_str.startswith("P3") else "LOW"

    standardized_incident = {
        "incident_type": "flood_emergency",
        "priority": prio_level_upper,
        "caller_name": citizen_profile.name or "Anonymous Caller",
        "phone_number": citizen_profile.phone,
        "location": f"{lat:.4f}, {lng:.4f}" if (lat and lng) else (extraction.location_description or "Unknown"),
        "landmark": extraction.location_description,
        "people_affected": extraction.people_count or 1,
        "children": extraction.children_count or 0,
        "elderly": extraction.elderly_count or 0,
        "injured": bool(extraction.injured),
        "trapped": bool(extraction.trapped),
        "immediate_danger": bool(extraction.safe is False or extraction.trapped or prio_level_upper in ["CRITICAL", "HIGH"]),
        "description": raw_transcript or translated_transcript or "Flood emergency call",
        "language": target_lang,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "NEW"
    }

    # Step 7: Synthesize Response Voice Audio (Sarvam Bulbul TTS in caller language)
    confirm_lang = target_lang if "-" in target_lang else f"{target_lang}-IN"
    lang_key = "kn" if target_lang.startswith("kn") else ("hi" if target_lang.startswith("hi") else "en")

    from services.conversation_manager import CONFIRMATION_MESSAGES, SAFETY_INSTRUCTIONS
    conf_key = "critical" if prio_level_upper == "CRITICAL" else "standard"
    conf_text = CONFIRMATION_MESSAGES[conf_key].get(lang_key, CONFIRMATION_MESSAGES[conf_key]["kn"])
    safety_text = SAFETY_INSTRUCTIONS.get(lang_key, SAFETY_INSTRUCTIONS["kn"])
    full_response_text = f"{conf_text} {safety_text}"

    tts_result = sarvam_tts.synthesize_speech(
        text=full_response_text,
        language=confirm_lang,
    )

    return VoiceProcessResponse(
        success=True,
        sos_id=incident_report.id,
        session_id=session_id,
        source="VOICE_CALL",
        citizen_id=citizen_profile.id,
        citizen_name=citizen_profile.name,
        citizen_matched=citizen_profile.is_verified,
        language=target_lang,
        transcript=raw_transcript,
        translated_transcript=translated_transcript,
        incident=extraction,
        priority={
            "score": priority_res["score"],
            "level": mapped_level,
            "canonical_level": prio_level_str,
            "reason": priority_res.get("reason"),
            "emergency_signals": priority_res.get("emergency_signals", []),
            "vulnerability_signals": priority_res.get("vulnerability_signals", []),
            "reasons": canonical_priority["reason"],
            "risk": canonical_incident["risk"],
        },
        assigned_shelter=shelter_obj.name if shelter_obj else None,
        emergency_incident=standardized_incident,
        safety_instruction=safety_text,
        response_speech_text=full_response_text,
        response_audio_base64=tts_result.get("audio_base64"),
        status="PENDING_RESCUE",
    )


# ---------------------------------------------------------------------------
# 2. Telephony Incoming Call Webhook: POST /api/voice/incoming
# ---------------------------------------------------------------------------

def _get_twiml_lang_code(lang: str) -> str:
    """Maps language codes to Twilio <Say> and <Gather> speech recognition language codes."""
    if lang.startswith("kn"):
        return "kn-IN"
    elif lang.startswith("hi"):
        return "hi-IN"
    elif lang.startswith("te"):
        return "te-IN"
    elif lang.startswith("ta"):
        return "ta-IN"
    return "en-IN"


@router.post("/voice/incoming")
@router.post("/twilio/voice-gather")
@router.post("/api/voice/incoming")
async def voice_incoming_call(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Telephony webhook for incoming calls using Speech Recognition TwiML (<Say> and <Gather>).
    Detects caller phone, matches citizen, initializes VoiceSession, returns initial emergency greeting
    and listens for caller's spoken response via Twilio speech recognition.
    """
    form_data = {}
    try:
        form_data = await request.form()
    except Exception:
        pass

    caller_phone = form_data.get("From") or form_data.get("Caller") or form_data.get("caller_phone") or "UNKNOWN"
    call_sid = form_data.get("CallSid") or form_data.get("call_id") or f"call_{uuid.uuid4().hex[:10]}"

    agent_lang = os.getenv("AGENT_LANGUAGE", "kn-IN")
    if agent_lang == "unknown":
        agent_lang = "kn-IN"

    # Match caller against registered citizens
    citizen, is_matched = match_citizen_by_phone(db, str(caller_phone), language=agent_lang)

    # Greeting based on language
    if agent_lang.startswith("kn"):
        greeting_text = (
            f"ನಮಸ್ಕಾರ {citizen.name or ''}, ಫಿರಾ ತುರ್ತು ರಕ್ಷಣಾ ಸಹಾಯವಾಣಿಗೆ ಸ್ವಾಗತ. ನೀವು ಸುರಕ್ಷಿತವಾಗಿದ್ದೀರಾ? ನಿಮ್ಮ ತುರ್ತು ಪರಿಸ್ಥಿತಿಯನ್ನು ತಿಳಿಸಿ."
            if citizen.is_verified
            else "ನಮಸ್ಕಾರ, ಫಿರಾ ಪ್ರವಾಹ ತುರ್ತು ರಕ್ಷಣಾ ಸಹಾಯವಾಣಿಗೆ ಸ್ವಾಗತ. ನೀವು ಸುರಕ್ಷಿತವಾಗಿದ್ದೀರಾ? ನಿಮ್ಮ ಪರಿಸ್ಥಿತಿಯನ್ನು ತಿಳಿಸಿ."
        )
    elif agent_lang.startswith("hi"):
        greeting_text = (
            f"नमस्ते {citizen.name or ''}, फिरा बाढ़ आपातकालीन सहायता में आपका स्वागत है। क्या आप सुरक्षित हैं? अपनी स्थिति बताएं।"
            if citizen.is_verified
            else "नमस्ते, फिरा बाढ़ आपातकालीन सहायता में आपका स्वागत है। क्या आप सुरक्षित हैं? कृपया अपनी स्थिति बताएं।"
        )
    else:
        greeting_text = (
            f"Namaskara {citizen.name or ''}, welcome to FIRA Flood Emergency Helpline. Are you safe right now? Please state your emergency."
            if citizen.is_verified
            else "Namaskara, welcome to FIRA Flood Emergency Helpline. Are you safe right now? Please describe your situation."
        )

    # Initialize active session in conversation manager
    session = get_or_create_session(
        session_id=call_sid,
        caller_phone=citizen.phone,
        citizen_id=citizen.id,
        language=agent_lang,
    )
    session.turns.append({"role": "agent", "text": greeting_text})

    base_url = get_public_base_url(request)
    action_url = f"{base_url}/voice/turn"
    twiml_lang = _get_twiml_lang_code(agent_lang)

    # Twilio Speech Recognition TwiML: <Say> inside <Gather> plays speech to the caller and captures their voice
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" timeout="5" speechTimeout="auto" action="{action_url}" method="POST" language="{twiml_lang}">
        <Say language="{twiml_lang}">{greeting_text}</Say>
    </Gather>
    <Say language="{twiml_lang}">ನಿಮ್ಮ ಧ್ವನಿ ಕೇಳಿಸಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೊಮ್ಮೆ ಮಾತನಾಡಿ.</Say>
    <Redirect method="POST">{base_url}/voice/incoming</Redirect>
</Response>"""

    return Response(content=twiml_response, media_type="application/xml")


# ---------------------------------------------------------------------------
# 3. Conversational Turn Handler: POST /api/voice/turn & POST /voice/turn
# ---------------------------------------------------------------------------

class TurnRequest(BaseModel):
    session_id: str
    caller_input: str
    language: Optional[str] = "kn-IN"


class OutboundCallRequest(BaseModel):
    to: Optional[str] = None
    from_number: Optional[str] = None
    language: Optional[str] = None


def _initial_voice_greeting(citizen: VoiceCitizenProfile, language: str) -> str:
    """Returns the first prompt used for inbound and outbound speech calls."""
    if language.startswith("kn"):
        return (
            f"ನಮಸ್ಕಾರ {citizen.name or ''}, ಫಿರಾ ತುರ್ತು ರಕ್ಷಣಾ ಸಹಾಯವಾಣಿಗೆ ಸ್ವಾಗತ. ನೀವು ಸುರಕ್ಷಿತವಾಗಿದ್ದೀರಾ? ನಿಮ್ಮ ತುರ್ತು ಪರಿಸ್ಥಿತಿಯನ್ನು ತಿಳಿಸಿ."
            if citizen.is_verified
            else "ನಮಸ್ಕಾರ, ಫಿರಾ ಪ್ರವಾಹ ತುರ್ತು ರಕ್ಷಣಾ ಸಹಾಯವಾಣಿಗೆ ಸ್ವಾಗತ. ನೀವು ಸುರಕ್ಷಿತವಾಗಿದ್ದೀರಾ? ನಿಮ್ಮ ಪರಿಸ್ಥಿತಿಯನ್ನು ತಿಳಿಸಿ."
        )
    if language.startswith("hi"):
        return (
            f"नमस्ते {citizen.name or ''}, फिरा बाढ़ आपातकालीन सहायता में आपका स्वागत है। क्या आप सुरक्षित हैं? अपनी स्थिति बताएं।"
            if citizen.is_verified
            else "नमस्ते, फिरा बाढ़ आपातकालीन सहायता में आपका स्वागत है। क्या आप सुरक्षित हैं? कृपया अपनी स्थिति बताएं।"
        )
    return (
        f"Namaskara {citizen.name or ''}, welcome to FIRA Flood Emergency Helpline. Are you safe right now? Please state your emergency."
        if citizen.is_verified
        else "Namaskara, welcome to FIRA Flood Emergency Helpline. Please describe your emergency."
    )


def _speech_gather_twiml(prompt: str, action_url: str, language: str) -> str:
    """Builds escaped TwiML for a single speech-recognition turn."""
    twiml_lang = _get_twiml_lang_code(language)
    safe_prompt = html.escape(prompt, quote=False)
    safe_action = html.escape(action_url, quote=True)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" timeout="5" speechTimeout="auto" action="{safe_action}" method="POST" language="{twiml_lang}">
        <Say language="{twiml_lang}">{safe_prompt}</Say>
    </Gather>
    <Say language="{twiml_lang}">Please tell us what happened.</Say>
    <Redirect method="POST">{safe_action}</Redirect>
</Response>'''


@router.post("/voice/call")
async def create_outbound_voice_call(body: OutboundCallRequest):
    """Starts a Twilio outbound call whose answer URL begins speech recognition."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    to_number = (body.to or os.getenv("TWILIO_TO_NUMBER", "")).strip()
    from_number = (body.from_number or os.getenv("TWILIO_PHONE_NUMBER", "")).strip()
    public_url = os.getenv("PUBLIC_URL", "").strip().rstrip("/")

    if not account_sid or not auth_token:
        raise HTTPException(status_code=503, detail="Twilio credentials are not configured")
    if not to_number or not from_number:
        raise HTTPException(status_code=400, detail="Both to and from phone numbers are required")
    if not public_url:
        raise HTTPException(status_code=503, detail="PUBLIC_URL is required for the Twilio webhook")

    payload = {
        "To": to_number,
        "From": from_number,
        "Url": f"{public_url}/voice/outbound",
    }
    if os.getenv("TWILIO_STATUS_CALLBACK_URL", "").strip():
        payload["StatusCallback"] = os.getenv("TWILIO_STATUS_CALLBACK_URL", "").strip()

    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            twilio_response = await client.post(endpoint, data=payload, auth=(account_sid, auth_token))
    except httpx.HTTPError as exc:
        logger.error("Twilio call request failed: %s", exc)
        raise HTTPException(status_code=502, detail="Unable to reach Twilio") from exc

    if twilio_response.status_code >= 400:
        logger.error("Twilio rejected outbound call: status=%s body=%s", twilio_response.status_code, twilio_response.text)
        raise HTTPException(status_code=502, detail="Twilio rejected the outbound call")

    response_data = twilio_response.json()
    return {
        "success": True,
        "call_sid": response_data.get("sid"),
        "status": response_data.get("status"),
        "to": to_number,
    }


@router.post("/voice/outbound")
async def outbound_voice_webhook(request: Request, db: Session = Depends(get_db)):
    """Answers an outbound Twilio call with the initial speech-recognition prompt."""
    form_data = await request.form()
    caller_phone = str(form_data.get("To") or form_data.get("Called") or "UNKNOWN")
    call_sid = str(form_data.get("CallSid") or f"call_{uuid.uuid4().hex[:10]}")
    language = os.getenv("AGENT_LANGUAGE", "kn-IN")
    if language == "unknown":
        language = "kn-IN"

    citizen, _ = match_citizen_by_phone(db, caller_phone, language=language)
    session = get_or_create_session(
        session_id=call_sid,
        caller_phone=citizen.phone,
        citizen_id=citizen.id,
        language=language,
    )
    greeting = _initial_voice_greeting(citizen, language)
    session.turns.append({"role": "agent", "text": greeting})
    base_url = get_public_base_url(request)
    twiml = _speech_gather_twiml(greeting, f"{base_url}/voice/turn", language)
    return Response(content=twiml, media_type="application/xml")


@router.post("/voice/turn")
@router.post("/api/voice/turn")
async def voice_conversation_turn(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Processes a conversational turn in a multi-turn call.
    Supports both JSON request and Twilio Speech Recognition webhook callbacks (<Gather>).
    """
    content_type = request.headers.get("content-type", "")
    is_json = "application/json" in content_type

    if is_json:
        body = await request.json()
        session_id = body.get("session_id")
        user_text = body.get("caller_input", "")
        lang = body.get("language", "kn-IN")
        caller_phone = body.get("caller_phone", "UNKNOWN")
    else:
        form = await request.form()
        session_id = form.get("CallSid") or form.get("session_id") or str(uuid.uuid4())
        # Twilio sends transcribed speech in 'SpeechResult'
        user_text = form.get("SpeechResult") or form.get("caller_input") or ""
        caller_phone = form.get("From") or "UNKNOWN"
        existing = get_or_create_session(session_id=session_id)
        lang = form.get("Language") or (existing.language if existing.language and existing.language != "unknown" else os.getenv("AGENT_LANGUAGE", "kn-IN"))
        if lang == "unknown":
            lang = "kn-IN"

    session = get_or_create_session(
        session_id=session_id,
        caller_phone=str(caller_phone),
        language=lang,
    )

    if user_text:
        session.turns.append({"role": "caller", "text": user_text})
        session.raw_transcripts.append(user_text)

        # Translate if non-English
        translated_chunk = user_text
        if not lang.startswith("en") and sarvam_stt.api_key:
            try:
                trans_res = sarvam_stt.translate_text(user_text, source_lang=lang)
                translated_chunk = trans_res.get("translated_text", user_text)
            except Exception:
                pass
        session.translated_transcripts.append(translated_chunk)

        # Extract facts from cumulative or current transcript
        cumulative_text = " ".join(session.translated_transcripts)
        new_facts = gemini_extractor.extract_emergency_facts(cumulative_text, " ".join(session.raw_transcripts))
        session.update_from_extraction(new_facts)

    # Automatically persist incident report so Command Center updates immediately
    try:
        commit_voice_session_incident(db, session)
    except Exception as exc:
        logger.error("Error committing voice incident in turn: %s", exc)

    # Determine next question or completion
    next_question = session.get_next_question()
    base_url = get_public_base_url(request)
    twiml_lang = _get_twiml_lang_code(lang)

    # Check if conversation finished
    if not next_question or session.is_completed:
        facts = session.extracted_data
        prio_upper = "CRITICAL" if (facts.trapped or facts.injured or (facts.people_count and facts.people_count > 3)) else "STANDARD"
        conf_key = "critical" if prio_upper == "CRITICAL" else "standard"
        lang_key = "kn" if lang.startswith("kn") else ("hi" if lang.startswith("hi") else "en")
        conf_text = CONFIRMATION_MESSAGES[conf_key].get(lang_key, CONFIRMATION_MESSAGES[conf_key]["kn"])
        safety_text = SAFETY_INSTRUCTIONS.get(lang_key, SAFETY_INSTRUCTIONS["kn"])
        final_reply = f"{conf_text} {safety_text}"
        session.turns.append({"role": "agent", "text": final_reply})
        session.status = "COMPLETED"
        session.ended_at = datetime.now(timezone.utc)

        if not is_json:
            twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="{twiml_lang}">{final_reply}</Say>
    <Hangup/>
</Response>"""
            return Response(content=twiml_response, media_type="application/xml")

        return {
            "session_id": session_id,
            "is_completed": True,
            "next_question": None,
            "final_message": final_reply,
            "extracted_so_far": session.extracted_data.model_dump(),
        }

    # Still in progress - ask next question
    session.turns.append({"role": "agent", "text": next_question})

    if not is_json:
        action_url = f"{base_url}/voice/turn"
        twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" timeout="5" speechTimeout="auto" action="{action_url}" method="POST" language="{twiml_lang}">
        <Say language="{twiml_lang}">{next_question}</Say>
    </Gather>
    <Say language="{twiml_lang}">ದಯವಿಟ್ಟು ಉತ್ತರಿಸಿ.</Say>
    <Redirect method="POST">{action_url}</Redirect>
</Response>"""
        return Response(content=twiml_response, media_type="application/xml")

    # Synthesize audio for JSON clients
    tts = sarvam_tts.synthesize_speech(next_question, language=lang)
    return {
        "session_id": session_id,
        "is_completed": session.is_completed,
        "next_question": next_question,
        "response_audio_base64": tts.get("audio_base64"),
        "extracted_so_far": session.extracted_data.model_dump(),
    }


# ---------------------------------------------------------------------------
# 4. Finalize Call & Commit SOS: POST /api/voice/finalize
# ---------------------------------------------------------------------------

class FinalizeRequest(BaseModel):
    session_id: str


@router.post("/voice/finalize")
async def finalize_voice_call(
    body: FinalizeRequest,
    db: Session = Depends(get_db),
):
    """
    Finalizes an active conversational voice session, creates the FIRA incident Report,
    computes deterministic priority score, and records auditable session details.
    """
    session = get_or_create_session(session_id=body.session_id)
    session.ended_at = datetime.now(timezone.utc)
    session.status = "COMPLETED"

    raw_transcript = " ".join(session.raw_transcripts)
    translated_transcript = " ".join(session.translated_transcripts)

    # Match citizen
    citizen_profile, is_matched = match_citizen_by_phone(db, session.caller_phone, language=session.language)

    # Merge facts
    normalized_sos = merge_voice_sos(
        citizen=citizen_profile,
        extraction=session.extracted_data,
        session_id=session.session_id,
        transcript=raw_transcript,
        translated_transcript=translated_transcript,
        language=session.language,
    )

    derived_severity = calculate_derived_severity(session.extracted_data)
    lat = normalized_sos.location.get("lat")
    lng = normalized_sos.location.get("lng")
    env_risk = _get_zone_risk(db, lat, lng)

    # Synthesize description for Priority Engine
    full_description = f"{raw_transcript} {session.extracted_data.additional_information or ''}"
    if session.extracted_data.trapped:
        full_description += " trapped unable to leave"
    if session.extracted_data.injured or session.extracted_data.medical_emergency:
        full_description += " injured medical emergency"

    sev_str = {1: "Low", 2: "Low", 3: "Medium", 4: "High", 5: "Critical"}.get(derived_severity, "Medium")
    priority_res = compute_priority(
        severity=sev_str,
        description=full_description,
        zone_risk_score=env_risk,
        lat=lat,
        lng=lng,
    )
    normalized_priority_score = round(priority_res["score"] / 100.0, 3)
    nearest_shelter_id = _get_nearest_shelter_id(db, lat, lng)

    # Ensure VoiceSession exists first before Report references it
    voice_rec = db.query(VoiceSession).filter(VoiceSession.id == session.session_id).first()
    if not voice_rec:
        voice_rec = VoiceSession(
            id=session.session_id,
            citizen_id=citizen_profile.id,
            caller_phone=session.caller_phone,
            language=session.language,
            started_at=session.started_at,
            ended_at=session.ended_at,
            transcript=raw_transcript,
            translated_transcript=translated_transcript,
            extracted_data=json.dumps(session.extracted_data.model_dump()),
            conversation_history=json.dumps(session.turns),
            status="PROCESSING",
            incident_id=None,
        )
        db.add(voice_rec)
        db.flush()
    else:
        voice_rec.transcript = raw_transcript
        voice_rec.translated_transcript = translated_transcript
        voice_rec.extracted_data = json.dumps(session.extracted_data.model_dump())
        voice_rec.conversation_history = json.dumps(session.turns)
        voice_rec.ended_at = session.ended_at
        db.flush()

    # Save to Neon Report
    incident = Report(
        name=citizen_profile.name or f"Voice Caller ({session.caller_phone})",
        phone=citizen_profile.phone,
        reported_by=citizen_profile.id,
        lat=lat if lat is not None else 12.9716,
        lng=lng if lng is not None else 77.5946,
        severity=derived_severity,
        description=f"[VOICE SOS] {raw_transcript}\n(Translated: {translated_transcript})",
        flood_type="FLOOD",
        water_depth_m=session.extracted_data.water_depth_m,
        people_affected=session.extracted_data.people_count or 1,
        people_trapped=session.extracted_data.people_count if session.extracted_data.trapped else (1 if session.extracted_data.trapped else 0),
        medical_emergency=bool(session.extracted_data.medical_emergency or session.extracted_data.injured),
        priority_score=normalized_priority_score,
        status="prioritized",
        shelter_id=nearest_shelter_id,
        source="VOICE_CALL",
        voice_session_id=session.session_id,
    )
    db.add(incident)
    db.flush()

    voice_rec.incident_id = incident.id
    voice_rec.status = "SOS_CREATED"
    db.commit()

    return {
        "success": True,
        "sos_id": incident.id,
        "session_id": session.session_id,
        "source": "VOICE_CALL",
        "priority_score": normalized_priority_score,
        "priority_level": priority_level(normalized_priority_score),
        "status": "PENDING_RESCUE",
    }


# ---------------------------------------------------------------------------
# 5. Command Center Review: GET /api/voice/sessions & GET /api/voice/sessions/{id}
# ---------------------------------------------------------------------------

@router.get("/voice/sessions", response_model=List[VoiceSessionDetail])
def list_voice_sessions(
    db: Session = Depends(get_db),
):
    """Lists all voice call sessions for Command Center review and auditing."""
    sessions = db.query(VoiceSession).order_by(VoiceSession.started_at.desc()).all()
    results = []
    for s in sessions:
        extracted = None
        if s.extracted_data:
            try:
                extracted = json.loads(s.extracted_data)
            except Exception:
                pass
        results.append(
            VoiceSessionDetail(
                id=s.id,
                citizen_id=s.citizen_id,
                caller_phone=s.caller_phone,
                language=s.language,
                started_at=s.started_at,
                ended_at=s.ended_at,
                transcript=s.transcript,
                translated_transcript=s.translated_transcript,
                extracted_data=extracted,
                status=s.status,
                incident_id=s.incident_id,
            )
        )
    return results


@router.get("/voice/sessions/{session_id}", response_model=VoiceSessionDetail)
def get_voice_session(
    session_id: str,
    db: Session = Depends(get_db),
):
    """Retrieves a single voice session with full conversation history and extracted facts."""
    session = db.query(VoiceSession).filter(VoiceSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Voice session not found")

    extracted = None
    if session.extracted_data:
        try:
            extracted = json.loads(session.extracted_data)
        except Exception:
            pass

    return VoiceSessionDetail(
        id=session.id,
        citizen_id=session.citizen_id,
        caller_phone=session.caller_phone,
        language=session.language,
        started_at=session.started_at,
        ended_at=session.ended_at,
        transcript=session.transcript,
        translated_transcript=session.translated_transcript,
        extracted_data=extracted,
        status=session.status,
        incident_id=session.incident_id,
    )


# ---------------------------------------------------------------------------
# 6. Audio Helpers & Telephony Media Conversion
# ---------------------------------------------------------------------------

def _mulaw_to_wav(mulaw_data: bytes, sample_rate: int = 8000) -> bytes:
    """Converts 8kHz 8-bit mu-law raw audio to 16-bit PCM WAV."""
    if audioop is not None:
        pcm = audioop.ulaw2lin(mulaw_data, 2)
    else:
        pcm = bytes(len(mulaw_data) * 2)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _wav_to_mulaw(wav_bytes: bytes, target_sample_rate: int = 8000) -> bytes:
    """Converts WAV audio bytes to 8kHz 8-bit mu-law payload."""
    if not wav_bytes:
        return b""
    try:
        buf = io.BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            pcm = wf.readframes(wf.getnframes())
            sr = wf.getframerate()
            sw = wf.getsampwidth()

        if audioop is not None:
            if sw == 1:
                pcm = audioop.lin2lin(pcm, 1, 2)
            if sr != target_sample_rate:
                pcm, _ = audioop.ratecv(pcm, 2, 1, sr, target_sample_rate, None)
            return audioop.lin2ulaw(pcm, 2)
        return bytes(len(pcm) // 2)
    except Exception as exc:
        logger.error("Error converting WAV to mu-law: %s", exc)
        return b""


def commit_voice_session_incident(db: Session, session: VoiceSessionState) -> Optional[Report]:
    """Creates or updates a FIRA Report and VoiceSession record from the active conversation state."""
    raw_transcript = " ".join(session.raw_transcripts).strip()
    translated_transcript = " ".join(session.translated_transcripts).strip()

    citizen_profile, is_matched = match_citizen_by_phone(db, session.caller_phone, language=session.language)
    normalized_sos = merge_voice_sos(
        citizen=citizen_profile,
        extraction=session.extracted_data,
        session_id=session.session_id,
        transcript=raw_transcript,
        translated_transcript=translated_transcript,
        language=session.language,
    )

    derived_severity = calculate_derived_severity(session.extracted_data)
    lat = normalized_sos.location.get("lat")
    lng = normalized_sos.location.get("lng")
    env_risk = _get_zone_risk(db, lat, lng)

    full_description = f"[VOICE SOS - {session.language}] {raw_transcript or 'Emergency voice call'}\n(Translation: {translated_transcript or 'N/A'})"
    if session.extracted_data.trapped:
        full_description += " trapped unable to leave"
    if session.extracted_data.injured or session.extracted_data.medical_emergency:
        full_description += " injured medical emergency"

    sev_str = {1: "Low", 2: "Low", 3: "Medium", 4: "High", 5: "Critical"}.get(derived_severity, "Medium")
    priority_res = compute_priority(
        severity=sev_str,
        description=full_description,
        zone_risk_score=env_risk,
        lat=lat,
        lng=lng,
    )
    normalized_priority_score = round(priority_res["score"] / 100.0, 3)
    nearest_shelter_id = _get_nearest_shelter_id(db, lat, lng)

    # Ensure VoiceSession exists first before Report references it
    vrec = db.query(VoiceSession).filter(VoiceSession.id == session.session_id).first()
    if not vrec:
        vrec = VoiceSession(
            id=session.session_id,
            citizen_id=citizen_profile.id,
            caller_phone=session.caller_phone,
            language=session.language,
            started_at=session.started_at,
            ended_at=session.ended_at,
            transcript=raw_transcript,
            translated_transcript=translated_transcript,
            extracted_data=json.dumps(session.extracted_data.model_dump()),
            conversation_history=json.dumps(session.turns),
            status="IN_PROGRESS",
            incident_id=None,
        )
        db.add(vrec)
        db.flush()
    else:
        vrec.transcript = raw_transcript
        vrec.translated_transcript = translated_transcript
        vrec.extracted_data = json.dumps(session.extracted_data.model_dump())
        vrec.conversation_history = json.dumps(session.turns)
        vrec.status = session.status
        vrec.ended_at = session.ended_at or datetime.now(timezone.utc)
        db.flush()

    # Check if a report already exists for this call session
    report = db.query(Report).filter(Report.voice_session_id == session.session_id).first()
    if not report:
        report = Report(
            name=citizen_profile.name or f"Voice Caller ({session.caller_phone})",
            phone=citizen_profile.phone,
            reported_by=citizen_profile.id,
            lat=lat if lat is not None else 12.9716,
            lng=lng if lng is not None else 77.5946,
            severity=derived_severity,
            description=full_description,
            flood_type="FLOOD",
            water_depth_m=session.extracted_data.water_depth_m,
            people_affected=session.extracted_data.people_count or 1,
            people_trapped=session.extracted_data.people_count if session.extracted_data.trapped else (1 if session.extracted_data.trapped else 0),
            medical_emergency=bool(session.extracted_data.medical_emergency or session.extracted_data.injured),
            priority_score=normalized_priority_score,
            status="prioritized",
            shelter_id=nearest_shelter_id,
            source="VOICE_CALL",
            voice_session_id=session.session_id,
        )
        db.add(report)
        db.flush()
    else:
        # Update existing report with latest intelligence
        report.severity = derived_severity
        report.priority_score = normalized_priority_score
        report.description = full_description
        if lat is not None and lng is not None:
            report.lat = lat
            report.lng = lng
        if session.extracted_data.people_count:
            report.people_affected = session.extracted_data.people_count
        if session.extracted_data.trapped:
            report.people_trapped = session.extracted_data.people_count or 1
        if session.extracted_data.medical_emergency or session.extracted_data.injured:
            report.medical_emergency = True
        if nearest_shelter_id:
            report.shelter_id = nearest_shelter_id

    if report:
        vrec.incident_id = report.id
        vrec.status = "SOS_CREATED"

    db.commit()
    db.refresh(report)
    return report


def get_public_base_url(request: Request) -> str:
    """Resolves public base URL considering ngrok / reverse proxy headers or environment."""
    public_url = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if public_url:
        return public_url

    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_proto and forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}"

    host = request.headers.get("host")
    if host:
        proto = "https" if request.url.is_secure else "http"
        return f"{proto}://{host}"

    return str(request.base_url).rstrip("/")


# ---------------------------------------------------------------------------
# 7. Telephony Webhooks: Twilio & Vobiz (/twilio/voice, /answer, /hangup, /stream-status)
# ---------------------------------------------------------------------------

async def handle_telephony_answer(request: Request, db: Session, provider: str = "auto") -> Response:
    """
    Handles Twilio / Vobiz inbound voice webhook.
    Returns TwiML / XML initiating bidirectional audio WebSocket streaming to /ws.
    """
    form_data = {}
    try:
        form_data = await request.form()
    except Exception:
        pass

    caller_phone = form_data.get("From") or form_data.get("Caller") or "UNKNOWN"
    call_sid = form_data.get("CallSid") or form_data.get("CallUUID") or f"call_{uuid.uuid4().hex[:10]}"

    # Auto-detect provider if auto: Vobiz sends CallUUID, Twilio sends CallSid
    if provider == "auto":
        provider = "vobiz" if "CallUUID" in form_data else "twilio"

    # Look up registered citizen profile upfront
    citizen, is_matched = match_citizen_by_phone(db, str(caller_phone), language="unknown")
    logger.info("%s incoming call: phone=%s, citizen=%s, call_sid=%s", provider.upper(), caller_phone, citizen.name, call_sid)

    agent_lang = os.getenv("AGENT_LANGUAGE", "unknown")
    session = get_or_create_session(
        session_id=call_sid,
        caller_phone=citizen.phone,
        citizen_id=citizen.id,
        language=agent_lang,
    )

    base_url = get_public_base_url(request)
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"

    if provider == "vobiz":
        vobiz_ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
        xml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" keepCallAlive="true"
            contentType="audio/x-mulaw;rate=8000"
            statusCallbackUrl="{base_url}/stream-status"
            statusCallbackMethod="POST">
        {vobiz_ws_url}
    </Stream>
    <Hangup/>
</Response>"""
    else:
        # Twilio requires a publicly reachable secure WebSocket (wss://) for Media Streams.
        safe_call_sid = html.escape(str(call_sid), quote=True)
        safe_caller_phone = html.escape(str(caller_phone), quote=True)
        twilio_ws_url = re.sub(r"^https?://", "wss://", base_url) + "/ws"
        xml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{twilio_ws_url}">
            <Parameter name="callSid" value="{safe_call_sid}"/>
            <Parameter name="callerPhone" value="{safe_caller_phone}"/>
        </Stream>
    </Connect>
</Response>"""

    return Response(content=xml_response, media_type="application/xml")


async def handle_vobiz_answer(request: Request, db: Session) -> Response:
    return await handle_telephony_answer(request, db, provider="vobiz")


async def handle_twilio_voice(request: Request, db: Session) -> Response:
    return await handle_telephony_answer(request, db, provider="twilio")


async def handle_telephony_hangup(request: Request, db: Session) -> Response:
    """Handles Twilio / Vobiz call status/hangup callback."""
    form_data = {}
    try:
        form_data = await request.form()
    except Exception:
        pass

    call_sid = form_data.get("CallSid") or form_data.get("CallUUID") or "UNKNOWN"
    logger.info("Telephony call hangup/status received for CallSid: %s", call_sid)

    session = get_or_create_session(session_id=call_sid)
    session.status = "COMPLETED"
    session.ended_at = datetime.now(timezone.utc)

    if session.raw_transcripts:
        try:
            commit_voice_session_incident(db, session)
        except Exception as exc:
            logger.error("Error finalizing incident on hangup: %s", exc)

    return Response(status_code=200, content="OK")


async def handle_vobiz_hangup(request: Request, db: Session) -> Response:
    return await handle_telephony_hangup(request, db)


async def handle_vobiz_stream_status(request: Request) -> Response:
    """Handles stream status callback."""
    form_data = {}
    try:
        form_data = await request.form()
    except Exception:
        pass
    logger.info("Telephony stream-status: CallSid=%s, status=%s", form_data.get("CallSid") or form_data.get("CallUUID"), form_data.get("StreamStatus") or form_data.get("CallStatus"))
    return Response(status_code=200, content="OK")


# Router-level webhook endpoints (Supports both Twilio & Vobiz)
@router.post("/twilio/voice")
@router.post("/api/twilio/voice")
@router.post("/voice/twilio")
async def twilio_voice_endpoint(request: Request, db: Session = Depends(get_db)):
    return await handle_twilio_voice(request, db)


@router.post("/answer")
@router.post("/voice/answer")
@router.post("/api/answer")
@router.post("/api/voice/answer")
async def voice_answer_endpoint(request: Request, db: Session = Depends(get_db)):
    return await handle_telephony_answer(request, db, provider="auto")


@router.post("/hangup")
@router.post("/voice/hangup")
@router.post("/api/hangup")
@router.post("/api/voice/hangup")
@router.post("/twilio/status")
@router.post("/api/twilio/status")
async def voice_hangup_endpoint(request: Request, db: Session = Depends(get_db)):
    return await handle_telephony_hangup(request, db)


@router.post("/stream-status")
@router.post("/api/stream-status")
async def voice_stream_status_endpoint(request: Request):
    return await handle_vobiz_stream_status(request)


# ---------------------------------------------------------------------------
# 8. Real-time Telephony Media Stream: TelephonyCallSession (Twilio & Vobiz)
# ---------------------------------------------------------------------------

class TelephonyCallSession:
    """Manages real-time bidirectional audio streaming with Twilio, Vobiz, and Sarvam AI."""

    def __init__(self, ws: WebSocket, db: Session):
        self.ws = ws
        self.db = db
        self.stream_sid: Optional[str] = None
        self.stream_id: Optional[str] = None
        self.call_id: Optional[str] = None
        self.caller_phone: str = "UNKNOWN"
        self.language: str = os.getenv("AGENT_LANGUAGE", "unknown")
        self.is_playing: bool = False
        self._processing: bool = False
        self.is_twilio: bool = True

        # VAD (Voice Activity Detection) tuning
        self._audio_buf = bytearray()
        self._silence_cnt = 0
        self._speech_cnt = 0
        self._is_speaking = False
        self.SILENCE_THRESHOLD = 200
        self.SILENCE_FRAMES = 35    # ~700ms silence triggers STT
        self.MIN_SPEECH_FRAMES = 6   # Minimum 120ms speech

    async def _send(self, data: str):
        await self.ws.send_text(data)

    async def handle_message(self, message_str: str):
        try:
            data = json.loads(message_str)
            event = data.get("event")

            # Twilio 'connected' event
            if event == "connected":
                self.is_twilio = True
                logger.info("Twilio stream connected protocol")
                return

            if event == "start":
                start_data = data.get("start", {})
                # Twilio's Media Streams start payload contains mediaFormat.encoding.
                # Vobiz sends streamId/CallUUID instead.
                self.is_twilio = bool(
                    start_data.get("mediaFormat")
                    or data.get("streamSid")
                    or start_data.get("streamSid")
                )
                self.stream_sid = data.get("streamSid") or start_data.get("streamSid")
                self.stream_id = data.get("streamId") or start_data.get("streamId") or self.stream_sid

                # Check if Twilio custom parameters or callSid passed
                custom_params = start_data.get("customParameters", {})
                self.call_id = (
                    custom_params.get("callSid")
                    or start_data.get("callSid")
                    or data.get("callSid")
                    or data.get("callId")
                    or start_data.get("callId")
                    or start_data.get("callUUID")
                    or f"call_{uuid.uuid4().hex[:10]}"
                )
                if custom_params.get("callerPhone"):
                    self.caller_phone = custom_params.get("callerPhone")

                logger.info("Telephony stream started: stream_sid=%s, call_id=%s, phone=%s", self.stream_sid or self.stream_id, self.call_id, self.caller_phone)

                session_state = get_or_create_session(
                    session_id=self.call_id,
                    caller_phone=self.caller_phone,
                    language=self.language,
                )

                # Multilingual greeting: "Namaskara, how can I help you?"
                greeting = "Namaskara, how can I help you?"
                if self.language.startswith("kn"):
                    greeting = "ನಮಸ್ಕಾರ, ಇದು ಪ್ರವಾಹ ತುರ್ತು ಸಹಾಯವಾಣಿ. ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?"
                elif self.language.startswith("hi"):
                    greeting = "नमस्ते, यह बाढ़ आपातकालीन हेल्पलाइन है। मैं आपकी कैसे मदद कर सकता हूँ?"

                session_state.turns.append({"role": "agent", "text": greeting})
                await self._speak(greeting)

            elif event == "media":
                if not self._processing:
                    media_data = data.get("media", {})
                    payload = media_data.get("payload", "")
                    if payload:
                        await self._handle_audio(base64.b64decode(payload))

            elif event in ("playedStream", "mark"):
                self.is_playing = False
                logger.info("Telephony audio playback mark/complete")

            elif event == "clearedAudio":
                self.is_playing = False

            elif event == "stop":
                logger.info("Telephony stream stopped for call: %s", self.call_id)
                if self.call_id:
                    session_state = get_or_create_session(session_id=self.call_id)
                    session_state.status = "COMPLETED"
                    session_state.ended_at = datetime.now(timezone.utc)
                    if session_state.raw_transcripts:
                        try:
                            commit_voice_session_incident(self.db, session_state)
                        except Exception as exc:
                            logger.error("Error finalizing session on stop: %s", exc)

        except Exception as exc:
            logger.error("Error in TelephonyCallSession handle_message: %s", exc)

    async def _handle_audio(self, mulaw_chunk: bytes):
        if audioop is not None:
            pcm = audioop.ulaw2lin(mulaw_chunk, 2)
            rms = audioop.rms(pcm, 2)
        else:
            rms = 0

        if rms > self.SILENCE_THRESHOLD:
            if not self._is_speaking and self.is_playing:
                await self._clear_audio()  # Barge-in interruption
            self._is_speaking = True
            self._speech_cnt += 1
            self._silence_cnt = 0
            self._audio_buf.extend(mulaw_chunk)
        elif self._is_speaking:
            self._silence_cnt += 1
            self._audio_buf.extend(mulaw_chunk)
            if self._silence_cnt >= self.SILENCE_FRAMES and self._speech_cnt >= self.MIN_SPEECH_FRAMES:
                audio = bytes(self._audio_buf)
                self._reset_vad()
                self._processing = True
                asyncio.create_task(self._process(audio))

    def _reset_vad(self):
        self._audio_buf.clear()
        self._is_speaking = False
        self._silence_cnt = 0
        self._speech_cnt = 0

    async def _process(self, mulaw_audio: bytes):
        try:
            if self.is_playing:
                await self._clear_audio()

            # 1. Convert mu-law to standard WAV
            wav_bytes = _mulaw_to_wav(mulaw_audio)

            # 2. Sarvam Saaras STT
            stt_res = sarvam_stt.process_audio(wav_bytes, language_code=self.language)
            transcript = stt_res.get("transcript") or ""
            detected_lang = stt_res.get("language_code") or self.language
            if detected_lang and detected_lang != "unknown":
                self.language = detected_lang

            if not transcript or not transcript.strip():
                logger.info("STT returned empty transcript; waiting for caller")
                return

            logger.info("Caller utterance (%s): %s", self.language, transcript)

            # 3. Update session turn
            session_state = get_or_create_session(
                session_id=self.call_id or "unknown",
                caller_phone=self.caller_phone,
                language=self.language,
            )
            session_state.turns.append({"role": "caller", "text": transcript})
            session_state.raw_transcripts.append(transcript)

            # Translate if non-English
            translated_chunk = transcript
            if not self.language.startswith("en") and sarvam_stt.api_key:
                try:
                    trans_res = sarvam_stt.translate_text(transcript, source_lang=self.language)
                    translated_chunk = trans_res.get("translated_text", transcript)
                except Exception:
                    pass
            session_state.translated_transcripts.append(translated_chunk)

            # 4. Gemini Factual Extraction & Priority Engine
            cumulative_text = " ".join(session_state.translated_transcripts)
            facts = gemini_extractor.extract_emergency_facts(
                transcript=cumulative_text,
                original_transcript=" ".join(session_state.raw_transcripts),
            )
            session_state.update_from_extraction(facts)

            # 5. Persist / update Report in Database
            try:
                commit_voice_session_incident(self.db, session_state)
            except Exception as exc:
                logger.error("Error committing incident report in stream: %s", exc)

            # 6. Generate empathetic response or safety instruction
            next_q = session_state.get_next_question()
            if not next_q or session_state.is_completed:
                prio_upper = "CRITICAL" if (facts.trapped or facts.injured or (facts.people_count and facts.people_count > 3)) else "STANDARD"
                conf_key = "critical" if prio_upper == "CRITICAL" else "standard"
                lang_key = "kn" if self.language.startswith("kn") else ("hi" if self.language.startswith("hi") else "en")
                conf_text = CONFIRMATION_MESSAGES[conf_key].get(lang_key, CONFIRMATION_MESSAGES[conf_key]["kn"])
                safety_text = SAFETY_INSTRUCTIONS.get(lang_key, SAFETY_INSTRUCTIONS["kn"])
                reply = f"{conf_text} {safety_text}"
            else:
                reply = next_q

            session_state.turns.append({"role": "agent", "text": reply})
            logger.info("Agent reply: %s", reply)

            # 7. Synthesize audio via Sarvam Bulbul TTS and stream back
            await self._speak(reply)

        except Exception as exc:
            logger.error("Error in TelephonyCallSession _process pipeline: %s", exc)
        finally:
            self._processing = False

    async def _speak(self, text: str):
        target_lang = self.language if self.language != "unknown" else "en-IN"
        tts = sarvam_tts.synthesize_speech(text, language=target_lang, sample_rate=8000)
        wav_b64 = tts.get("audio_base64", "")
        if wav_b64:
            try:
                wav_bytes = base64.b64decode(wav_b64)
                mulaw = _wav_to_mulaw(wav_bytes, target_sample_rate=8000)
                if not mulaw:
                    mulaw = b"\xff" * 160
                await self._play_audio(mulaw)
            except Exception as exc:
                logger.error("Error in _speak audio streaming: %s", exc)

    async def _play_audio(self, mulaw_data: bytes):
        self.is_playing = True
        stream_id = self.stream_sid or self.stream_id or ""
        try:
            for i in range(0, len(mulaw_data), 160):
                if not self.is_playing:
                    break
                chunk = mulaw_data[i:i + 160]
                payload = base64.b64encode(chunk).decode("ascii")

                # Twilio media packet
                await self._send(json.dumps({
                    "event": "media",
                    "streamSid": stream_id,
                    "media": {
                        "payload": payload,
                    },
                }))

                if not self.is_twilio:
                    await self._send(json.dumps({
                        "event": "playAudio",
                        "streamId": stream_id,
                        "media": {
                            "contentType": "audio/x-mulaw",
                            "sampleRate": 8000,
                            "payload": payload,
                        },
                    }))

                await asyncio.sleep(0.018)  # 18-20ms pacing

            if stream_id and self.is_playing:
                # Twilio mark event
                await self._send(json.dumps({
                    "event": "mark",
                    "streamSid": stream_id,
                    "mark": {"name": f"tts-{len(self._audio_buf)}"},
                }))
                if not self.is_twilio:
                    await self._send(json.dumps({
                        "event": "checkpoint",
                        "streamId": stream_id,
                        "name": f"tts-{len(self._audio_buf)}",
                    }))
        except Exception as exc:
            logger.error("Error playing audio chunk: %s", exc)
        finally:
            self.is_playing = False

    async def _clear_audio(self):
        stream_id = self.stream_sid or self.stream_id or ""
        if stream_id:
            try:
                # Twilio clear
                await self._send(json.dumps({
                    "event": "clear",
                    "streamSid": stream_id,
                }))
                if not self.is_twilio:
                    await self._send(json.dumps({
                        "event": "clearAudio",
                        "streamId": stream_id,
                    }))
            except Exception:
                pass
        self.is_playing = False
        logger.info("Barge-in: cleared audio stream")


VobizCallSession = TelephonyCallSession  # Backward compatibility alias


async def handle_telephony_websocket(websocket: WebSocket, db: Session):
    """Common WebSocket connection handler for Twilio & Vobiz media streaming."""
    await websocket.accept()
    logger.info("Telephony call connected to FIRA Voice Agent")
    session = TelephonyCallSession(websocket, db)
    try:
        while True:
            message_str = await websocket.receive_text()
            await session.handle_message(message_str)
    except WebSocketDisconnect:
        logger.info("Telephony WebSocket disconnected.")
    except Exception as exc:
        logger.error("Telephony WebSocket stream error: %s", exc)


handle_vobiz_websocket = handle_telephony_websocket  # Backward compatibility alias


@router.websocket("/voice/stream")
@router.websocket("/ws")
@router.websocket("/twilio/stream")
async def voice_media_stream(websocket: WebSocket):
    db = SessionLocal()
    try:
        await handle_telephony_websocket(websocket, db)
    finally:
        db.close()
