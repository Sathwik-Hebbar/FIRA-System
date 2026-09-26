"""
FIRA – Multilingual AI Voice SOS Reproducible Demo Runner (Phase 20).

Scenario:
A registered citizen (Ramesh, +91 98765 43210) calls the FIRA emergency helpline during a flood.
Citizen speaks in Kannada:
"ನಮ್ಮ ಮನೆಗೆ ನೀರು ತುಂಬಿಕೊಂಡಿದೆ. ನಾವು ನಾಲ್ಕು ಜನ ಇದ್ದೇವೆ. ಇಬ್ಬರು ಮಕ್ಕಳು ಇದ್ದಾರೆ. ನನ್ನ ತಂದೆಗೆ ಕಾಲಿಗೆ ಗಾಯವಾಗಿದೆ. ನಾವು ಮನೆಯಿಂದ ಹೊರಗೆ ಬರಲು ಆಗುತ್ತಿಲ್ಲ."
(Our house is flooded. There are four people with us. Two are children. My father has injured his leg and we cannot get out.)

Pipeline execution:
1. Receive call & initialize voice session
2. Deterministically identify caller using phone number against FIRA User database
3. Greet in Kannada
4. Transcribe Kannada speech via Sarvam Saaras
5. Convert speech into English representation
6. Extract strictly factual emergency information via Gemini 1.5 Flash structured output
7. Match & verify citizen identity
8. Merge profile + incident facts into NormalizedFIRASOS
9. Pass to EXISTING FIRA Priority Engine for deterministic risk scoring
10. Store incident report & voice session in database
11. Link to Command Center
12. Generate Kannada voice confirmation via Sarvam Bulbul TTS
13. Output final JSON contract
"""

import json
import os
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from database import Base, SessionLocal, engine, ensure_schema_migrations
from models import Report, Shelter, User, VoiceSession, Zone
from priority_engine import compute_priority, compute_report_priority, priority_level
from routes_voice import process_voice_sos
from schemas.voice_sos import VoiceProcessRequest
from seed_data import seed_if_empty


def run_demo():
    print("=" * 75)
    print("🌊 FIRA — MULTILINGUAL AI EMERGENCY VOICE SOS PIPELINE DEMO")
    print("=" * 75)

    # Step 0: Ensure DB is initialized and seeded
    Base.metadata.create_all(bind=engine)
    ensure_schema_migrations()
    db = SessionLocal()
    seed_if_empty(db)

    caller_phone = "+91 98765 43210"
    kannada_speech = (
        "ನಮ್ಮ ಮನೆಗೆ ನೀರು ತುಂಬಿಕೊಂಡಿದೆ. ನಾವು ನಾಲ್ಕು ಜನ ಇದ್ದೇವೆ. "
        "ಇಬ್ಬರು ಮಕ್ಕಳು ಇದ್ದಾರೆ. ನನ್ನ ತಂದೆಗೆ ಕಾಲಿಗೆ ಗಾಯವಾಗಿದೆ. ನಾವು ಮನೆಯಿಂದ ಹೊರಗೆ ಬರಲು ಆಗುತ್ತಿಲ್ಲ."
    )

    print(f"\n[1] 📞 Incoming Emergency Call Received:")
    print(f"    Caller Phone : {caller_phone}")
    print(f"    Language     : kn-IN (Kannada)")

    print(f"\n[2] 🎙️ Caller Audio / Speech Input (Kannada):")
    print(f'    "{kannada_speech}"')

    # Execute pipeline request
    req = VoiceProcessRequest(
        caller_phone=caller_phone,
        transcript=kannada_speech,
        language="kn-IN",
    )

    import asyncio
    response = asyncio.run(process_voice_sos(req=req, db=db))

    print(f"\n[3] 👤 Citizen Identity Resolution (Deterministic Phone Match):")
    print(f"    Citizen Matched : {response.citizen_matched}")
    print(f"    Citizen ID      : {response.citizen_id}")
    print(f"    Citizen Name    : {response.citizen_name}")

    print(f"\n[4] 🌐 Sarvam Saaras STT & Speech-to-English Translation:")
    print(f"    Raw Transcript        : {response.transcript}")
    print(f"    Translated Transcript : {response.translated_transcript}")

    print(f"\n[5] 🤖 Gemini 1.5 Structured Fact Extraction (Strictly Factual, No Hallucinations):")
    print(f"    Incident Type     : {response.incident.incident_type}")
    print(f"    Safe Status       : {response.incident.safe}")
    print(f"    Trapped           : {response.incident.trapped}")
    print(f"    Injured           : {response.incident.injured}")
    print(f"    Total People      : {response.incident.people_count}")
    print(f"    Children          : {response.incident.children_count}")
    print(f"    Elderly           : {response.incident.elderly_count}")
    print(f"    Medical Emergency : {response.incident.medical_emergency}")
    print(f"    Rescue Required   : {response.incident.rescue_required}")
    print(f"    Notes             : {response.incident.additional_information}")

    print(f"\n[6] ⚖️ EXISTING FIRA Priority Engine Calculation (Sole Source of Truth):")
    print(f"    Priority Score    : {response.priority['score']} / 100.0 (Normalized: {response.priority['normalized_score']})")
    print(f"    Priority Level    : {response.priority['level']}")
    print(f"    Reason            : {response.priority['reason']}")
    print(f"    Emergency Signals : {response.priority['emergency_signals']}")
    print(f"    Vulnerabilities   : {response.priority['vulnerability_signals']}")

    print(f"\n[7] 🏥 Emergency Resources Assigned:")
    print(f"    Assigned Shelter  : {response.assigned_shelter}")
    print(f"    Incident ID (SOS) : #{response.sos_id}")
    print(f"    Session ID        : {response.session_id}")

    print(f"\n[8] 🔊 Sarvam Bulbul TTS Voice Confirmation to Citizen (Kannada):")
    print(f'    Response Audio Msg: "{response.response_speech_text}"')
    print(f"    Audio Payload     : {len(response.response_audio_base64 or '')} base64 chars generated")

    print("\n" + "=" * 75)
    print("📦 EXACT FINAL NORMALIZED VOICE SOS JSON CONTRACT:")
    print("=" * 75)
    final_json = json.dumps(response.model_dump(), indent=2, ensure_ascii=False)
    print(final_json)

    db.close()
    return response


if __name__ == "__main__":
    run_demo()
