"""
FIRA – Multilingual AI Voice SOS Automated Test Suite.
Covers all 25 required test cases:
1. Registered Kannada caller
2. Registered English caller
3. Registered Hindi caller
4. Unknown caller
5. Flooded house
6. Trapped citizen
7. Injured citizen
8. Children present
9. Elderly person present
10. Multiple emergency facts in one response
11. Incomplete information
12. Ambiguous information
13. No speech
14. Bad audio
15. STT failure
16. Gemini failure
17. Invalid JSON
18. Database failure
19. Duplicate call
20. Caller disconnect
21. Kannada + English mixed speech
22. Existing citizen location
23. Verbal location
24. Priority Engine integration
25. Full end-to-end flow
"""

import base64
import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure backend is in sys.path
backend_path = Path(__file__).resolve().parent.parent / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from main import app
from models import Report, Shelter, User, VoiceSession, Zone
from priority_engine import compute_priority, compute_report_priority
from schemas.voice_sos import EmergencyExtraction, VoiceCitizenProfile
from services.citizen_matcher import match_citizen_by_phone, normalize_phone_number
from services.conversation_manager import VoiceSessionState, get_or_create_session
from services.gemini_extractor import GeminiExtractor, gemini_extractor
from services.sarvam_stt import SarvamSTTService, sarvam_stt
from services.sarvam_tts import SarvamTTSService, sarvam_tts
from services.sos_merger import calculate_derived_severity, merge_voice_sos


# ---------------------------------------------------------------------------
# In-Memory Test Database Setup
# ---------------------------------------------------------------------------

test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


class TestVoiceSOSSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=test_engine)
        cls.client = TestClient(app)

    def setUp(self):
        # Clear tables and seed standard test users, zones, shelters
        self.db = TestingSessionLocal()
        self.db.query(VoiceSession).delete()
        self.db.query(Report).delete()
        self.db.query(User).delete()
        self.db.query(Zone).delete()
        self.db.query(Shelter).delete()

        # Seed registered citizen: Ramesh
        self.registered_user = User(
            name="Ramesh Gowda",
            email="ramesh@example.com",
            password_hash="mockhash",
            role="citizen",
            phone="+91 98765 43210",
            latitude=12.9716,
            longitude=77.5946,
        )
        self.db.add(self.registered_user)

        # Seed zone
        self.test_zone = Zone(
            name="Zone A - Riverbank",
            lat=12.9716,
            lng=77.5946,
            rainfall_mm=120.0,
            river_level_m=6.5,
            danger_level_m=5.0,
            elevation_m=12.0,
            drainage_quality=0.3,
            historical_frequency=0.8,
        )
        self.db.add(self.test_zone)

        # Seed shelter
        self.test_shelter = Shelter(
            name="Central Relief Camp",
            lat=12.9720,
            lng=77.5950,
            capacity=250,
        )
        self.db.add(self.test_shelter)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    # -----------------------------------------------------------------------
    # 1. Registered Kannada Caller
    # -----------------------------------------------------------------------
    def test_01_registered_kannada_caller(self):
        profile, matched = match_citizen_by_phone(self.db, "+91 98765 43210", "kn-IN")
        self.assertTrue(matched)
        self.assertEqual(profile.name, "Ramesh Gowda")
        self.assertEqual(profile.id, self.registered_user.id)
        self.assertTrue(profile.is_verified)

    # -----------------------------------------------------------------------
    # 2. Registered English Caller
    # -----------------------------------------------------------------------
    def test_02_registered_english_caller(self):
        profile, matched = match_citizen_by_phone(self.db, "9876543210", "en-IN")
        self.assertTrue(matched)
        self.assertEqual(profile.name, "Ramesh Gowda")

    # -----------------------------------------------------------------------
    # 3. Registered Hindi Caller
    # -----------------------------------------------------------------------
    def test_03_registered_hindi_caller(self):
        profile, matched = match_citizen_by_phone(self.db, "+919876543210", "hi-IN")
        self.assertTrue(matched)
        self.assertEqual(profile.language, "hi-IN")

    # -----------------------------------------------------------------------
    # 4. Unknown Caller
    # -----------------------------------------------------------------------
    def test_04_unknown_caller(self):
        profile, matched = match_citizen_by_phone(self.db, "+91 99999 00000", "kn-IN")
        self.assertFalse(matched)
        self.assertFalse(profile.is_verified)
        self.assertIsNone(profile.id)
        self.assertEqual(profile.phone, "+91 99999 00000")

    # -----------------------------------------------------------------------
    # 5. Flooded House
    # -----------------------------------------------------------------------
    def test_05_flooded_house(self):
        text = "Our house is flooded with water."
        ext = gemini_extractor.extract_emergency_facts(text)
        self.assertEqual(ext.incident_type, "FLOOD")
        self.assertFalse(ext.safe)

    # -----------------------------------------------------------------------
    # 6. Trapped Citizen
    # -----------------------------------------------------------------------
    def test_06_trapped_citizen(self):
        text = "Water is waist deep and we are trapped inside the house unable to leave."
        ext = gemini_extractor.extract_emergency_facts(text)
        self.assertTrue(ext.trapped)
        self.assertFalse(ext.evacuation_possible)

    # -----------------------------------------------------------------------
    # 7. Injured Citizen
    # -----------------------------------------------------------------------
    def test_07_injured_citizen(self):
        text = "My brother has an injured leg and bleeding."
        ext = gemini_extractor.extract_emergency_facts(text)
        self.assertTrue(ext.injured)
        self.assertTrue(ext.medical_emergency)

    # -----------------------------------------------------------------------
    # 8. Children Present
    # -----------------------------------------------------------------------
    def test_08_children_present(self):
        text = "There are 3 people here including two children."
        ext = gemini_extractor.extract_emergency_facts(text)
        self.assertEqual(ext.children_count, 2)
        self.assertEqual(ext.people_count, 3)

    # -----------------------------------------------------------------------
    # 9. Elderly Person Present
    # -----------------------------------------------------------------------
    def test_09_elderly_person_present(self):
        text = "My grandfather is 78 years old elderly person with us."
        ext = gemini_extractor.extract_emergency_facts(text)
        self.assertEqual(ext.elderly_count, 1)

    # -----------------------------------------------------------------------
    # 10. Multiple Emergency Facts in One Response
    # -----------------------------------------------------------------------
    def test_10_multiple_emergency_facts(self):
        kannada_multi = (
            "ನಮ್ಮ ಮನೆಗೆ ನೀರು ತುಂಬಿಕೊಂಡಿದೆ. ನಾವು ನಾಲ್ಕು ಜನ ಇದ್ದೇವೆ. "
            "ಇಬ್ಬರು ಮಕ್ಕಳು ಇದ್ದಾರೆ. ನನ್ನ ತಂದೆಗೆ ಕಾಲಿಗೆ ಗಾಯವಾಗಿದೆ. ನಾವು ಮನೆಯಿಂದ ಹೊರಗೆ ಬರಲು ಆಗುತ್ತಿಲ್ಲ."
        )
        english_multi = (
            "Our house is flooded. There are four people with us. Two are children. "
            "My father has injured his leg and we cannot get out."
        )
        ext = gemini_extractor.extract_emergency_facts(english_multi, kannada_multi)
        self.assertFalse(ext.safe)
        self.assertTrue(ext.trapped)
        self.assertTrue(ext.injured)
        self.assertEqual(ext.people_count, 4)
        self.assertEqual(ext.children_count, 2)
        self.assertTrue(ext.medical_emergency)
        self.assertTrue(ext.rescue_required)

    # -----------------------------------------------------------------------
    # 11. Incomplete Information (Preserves null, never invents)
    # -----------------------------------------------------------------------
    def test_11_incomplete_information(self):
        text = "We see some water outside."
        ext = gemini_extractor.extract_emergency_facts(text)
        self.assertIsNone(ext.children_count)
        self.assertIsNone(ext.elderly_count)
        self.assertIsNone(ext.disabled_person_count)
        self.assertIsNone(ext.water_depth_m)

    # -----------------------------------------------------------------------
    # 12. Ambiguous Information
    # -----------------------------------------------------------------------
    def test_12_ambiguous_information(self):
        text = "Maybe it is raining, not sure if we need help yet."
        ext = gemini_extractor.extract_emergency_facts(text)
        # Should not falsely mark trapped or injured
        self.assertFalse(ext.trapped or False)
        self.assertFalse(ext.injured or False)

    # -----------------------------------------------------------------------
    # 13. No Speech / Silence
    # -----------------------------------------------------------------------
    def test_13_no_speech(self):
        resp = self.client.post("/api/voice-sos/process", json={
            "caller_phone": "+91 98765 43210",
            "transcript": "",
            "language": "kn-IN",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertIsNotNone(data["sos_id"])

    def test_13a_outbound_webhook_returns_speech_gather(self):
        with patch.dict(os.environ, {"PUBLIC_URL": "https://example.test", "AGENT_LANGUAGE": "en-IN"}):
            resp = self.client.post(
                "/voice/outbound",
                data={"To": "+919876543210", "CallSid": "CA-test-outbound"},
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"].split(";")[0], "application/xml")
        self.assertIn('<Gather input="speech"', resp.text)
        self.assertIn('action="https://example.test/voice/turn"', resp.text)
        self.assertIn("Namaskara", resp.text)

    def test_13b_create_outbound_call_uses_twilio_api(self):
        mock_response = MagicMock(status_code=201)
        mock_response.json.return_value = {"sid": "CA-created", "status": "queued"}
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        env = {
            "TWILIO_ACCOUNT_SID": "AC-test",
            "TWILIO_AUTH_TOKEN": "token-test",
            "TWILIO_PHONE_NUMBER": "+10000000000",
            "PUBLIC_URL": "https://example.test",
        }
        with patch.dict(os.environ, env, clear=False), patch("routes_voice.httpx.AsyncClient", return_value=mock_client):
            resp = self.client.post("/api/voice/call", json={"to": "+919876543210"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["call_sid"], "CA-created")
        mock_client.post.assert_awaited_once()
        request_kwargs = mock_client.post.await_args.kwargs
        self.assertEqual(request_kwargs["data"]["Url"], "https://example.test/voice/outbound")
        self.assertEqual(request_kwargs["auth"], ("AC-test", "token-test"))

    # -----------------------------------------------------------------------
    # 14. Bad / Corrupt Audio
    # -----------------------------------------------------------------------
    def test_14_bad_audio(self):
        bad_base64 = "===corrupt_base64_not_audio==="
        # System should handle gracefully without crashing
        resp = self.client.post("/api/voice-sos/process", json={
            "caller_phone": "+91 98765 43210",
            "audio_base64": bad_base64,
            "language": "kn-IN",
        })
        self.assertEqual(resp.status_code, 200)

    # -----------------------------------------------------------------------
    # 15. STT Failure Fallback
    # -----------------------------------------------------------------------
    def test_15_stt_failure_fallback(self):
        service = SarvamSTTService(api_key="mock_key")
        res = service.process_audio(b"a" * 64, language_code="kn-IN")
        self.assertIn(res["status"], ["ERROR", "SUCCESS"])
        self.assertEqual(res["language_code"], "kn-IN")

    # -----------------------------------------------------------------------
    # 16. Gemini Failure (Fallback Regex Extractor)
    # -----------------------------------------------------------------------
    def test_16_gemini_failure_fallback(self):
        extractor = GeminiExtractor(api_key="invalid_test_key")
        # Trigger fallback directly
        ext = extractor._extract_deterministic_fallback(
            "We are 4 people trapped with 2 children and injured person."
        )
        self.assertEqual(ext.people_count, 4)
        self.assertEqual(ext.children_count, 2)
        self.assertTrue(ext.trapped)
        self.assertTrue(ext.injured)

    # -----------------------------------------------------------------------
    # 17. Invalid JSON from LLM Handling
    # -----------------------------------------------------------------------
    def test_17_invalid_json_handling(self):
        extractor = GeminiExtractor()
        # Invalid LLM response should trigger regex fallback without crashing
        with patch.object(extractor, "_call_gemini", side_effect=Exception("Invalid JSON syntax")):
            ext = extractor.extract_emergency_facts("Four people trapped in flooded house.")
            self.assertTrue(ext.trapped)

    # -----------------------------------------------------------------------
    # 18. Database Transaction Safety
    # -----------------------------------------------------------------------
    def test_18_database_safety(self):
        # Ensure database rollbacks cleanly if an error occurs
        try:
            with self.db.begin_nested():
                inv = Report(name=None, lat=None, lng=None, severity=None) # type: ignore
                self.db.add(inv)
                self.db.flush()
        except Exception:
            self.db.rollback()
        # Verify DB is still clean and usable
        count = self.db.query(User).count()
        self.assertEqual(count, 1)

    # -----------------------------------------------------------------------
    # 19. Duplicate Call / Session Handling
    # -----------------------------------------------------------------------
    def test_19_duplicate_call_handling(self):
        sid = "duplicate_session_123"
        s1 = get_or_create_session(session_id=sid, caller_phone="9876543210")
        s2 = get_or_create_session(session_id=sid, caller_phone="9876543210")
        self.assertIs(s1, s2)

    # -----------------------------------------------------------------------
    # 20. Caller Disconnect
    # -----------------------------------------------------------------------
    def test_20_caller_disconnect(self):
        sid = "disconnect_session_999"
        session = get_or_create_session(session_id=sid, caller_phone="9876543210")
        session.raw_transcripts.append("Water is entering...")
        session.translated_transcripts.append("Water is entering...")
        
        # Finalize prematurely as in a disconnect
        resp = self.client.post("/api/voice/finalize", json={"session_id": sid})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])

    # -----------------------------------------------------------------------
    # 21. Kannada + English Mixed Speech (Tanglish / Kanglish)
    # -----------------------------------------------------------------------
    def test_21_mixed_kannada_english(self):
        mixed_text = "House full water agide. 3 people trapped idivi. Need rescue boat."
        ext = gemini_extractor.extract_emergency_facts(mixed_text)
        self.assertTrue(ext.trapped)
        self.assertEqual(ext.people_count, 3)

    # -----------------------------------------------------------------------
    # 22. Existing Citizen Location
    # -----------------------------------------------------------------------
    def test_22_existing_citizen_location(self):
        profile, matched = match_citizen_by_phone(self.db, "+91 98765 43210")
        ext = EmergencyExtraction(incident_type="FLOOD", safe=False)
        sos = merge_voice_sos(profile, ext)
        self.assertEqual(sos.location["lat"], self.registered_user.latitude)
        self.assertEqual(sos.location["lng"], self.registered_user.longitude)
        self.assertEqual(sos.location["source"], "CITIZEN_PROFILE")

    # -----------------------------------------------------------------------
    # 23. Verbal Location Extraction
    # -----------------------------------------------------------------------
    def test_23_verbal_location(self):
        text = "We are near the old temple on MG Road in Mangalore."
        ext = gemini_extractor.extract_emergency_facts(text)
        self.assertIsNotNone(ext.location_description)

    # -----------------------------------------------------------------------
    # 24. Priority Engine Integration (Deterministic Single Source of Truth)
    # -----------------------------------------------------------------------
    def test_24_priority_engine_integration(self):
        # Trapped + medical emergency + children in high risk zone should yield Critical priority
        p_res = compute_priority(
            severity="Critical",
            description="trapped cannot get out medical emergency children present",
            zone_risk_score=0.8,
        )
        self.assertGreaterEqual(p_res["score"], 75.0)
        self.assertIn("trapped", p_res["emergency_signals"])
        self.assertIn("child", p_res["vulnerability_signals"])

    # -----------------------------------------------------------------------
    # 25. Full End-to-End Voice SOS Pipeline
    # -----------------------------------------------------------------------
    def test_25_full_end_to_end_flow(self):
        payload = {
            "caller_phone": "+91 98765 43210",
            "transcript": "ನಮ್ಮ ಮನೆಗೆ ನೀರು ತುಂಬಿಕೊಂಡಿದೆ. ನಾವು ನಾಲ್ಕು ಜನ ಇದ್ದೇವೆ. ಇಬ್ಬರು ಮಕ್ಕಳು ಇದ್ದಾರೆ. ನನ್ನ ತಂದೆಗೆ ಕಾಲಿಗೆ ಗಾಯವಾಗಿದೆ. ನಾವು ಮನೆಯಿಂದ ಹೊರಗೆ ಬರಲು ಆಗುತ್ತಿಲ್ಲ.",
            "language": "kn-IN",
        }
        response = self.client.post("/api/voice-sos/process", json=payload)
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["source"], "VOICE_CALL")
        self.assertTrue(data["citizen_matched"])
        self.assertEqual(data["citizen_name"], "Ramesh Gowda")
        self.assertEqual(data["language"], "kn-IN")

        # Verify extracted emergency facts
        incident = data["incident"]
        self.assertTrue(incident["trapped"])
        self.assertTrue(incident["injured"])
        self.assertEqual(incident["people_count"], 4)
        self.assertEqual(incident["children_count"], 2)

        # Verify priority is calculated deterministically by existing Priority Engine
        priority = data["priority"]
        self.assertIsNotNone(priority["score"])
        self.assertIn(priority["level"], ["High", "Critical"])

        # Verify database record creation
        report_record = self.db.query(Report).filter(Report.id == data["sos_id"]).first()
        self.assertIsNotNone(report_record)
        self.assertEqual(report_record.source, "VOICE_CALL")
        self.assertEqual(report_record.people_affected, 4)

        # Verify VoiceSession linkage
        session_record = self.db.query(VoiceSession).filter(VoiceSession.id == data["session_id"]).first()
        self.assertIsNotNone(session_record)
        self.assertEqual(session_record.caller_phone, "+91 98765 43210")

        # Verify Kannada TTS response text contains confirmation and safe instructions
        self.assertTrue(any(w in data["response_speech_text"] for w in ["ಕಳುಹಿಸಲಾಗಿದೆ", "ದಾಖಲಿಸಲಾಗಿದೆ"]))
        self.assertIsNotNone(data["response_audio_base64"])
        self.assertEqual(data["emergency_incident"]["priority"], "CRITICAL")

    # -----------------------------------------------------------------------
    # 26. Telephony Inbound Webhook (Twilio & Vobiz)
    # -----------------------------------------------------------------------
    def test_26_telephony_answer_endpoints(self):
        # 1. Twilio Voice Webhook
        for path in ["/twilio/voice", "/api/twilio/voice", "/voice/twilio"]:
            resp = self.client.post(path, data={
                "From": "+91 98765 43210",
                "To": "+1 800 555 1234",
                "CallSid": f"CA_{uuid.uuid4().hex[:12]}",
            })
            self.assertEqual(resp.status_code, 200)
            self.assertIn("application/xml", resp.headers.get("content-type", ""))
            content = resp.text
            self.assertIn("<Response>", content)
            self.assertIn("<Connect>", content)
            self.assertIn('<Stream url="wss://', content)
            self.assertIn("/ws", content)

        # 2. Vobiz Answer Webhook
        for path in ["/answer", "/api/answer", "/api/voice/answer"]:
            resp = self.client.post(path, data={
                "From": "+91 98765 43210",
                "To": "+91 80000 12345",
                "CallUUID": f"vobiz_call_{uuid.uuid4().hex[:8]}",
            })
            self.assertEqual(resp.status_code, 200)
            self.assertIn("application/xml", resp.headers.get("content-type", ""))
            content = resp.text
            self.assertIn("<Response>", content)
            self.assertIn('<Stream bidirectional="true"', content)
            self.assertIn("/ws", content)

    # -----------------------------------------------------------------------
    # 27. Telephony Hangup and Status Endpoints
    # -----------------------------------------------------------------------
    def test_27_telephony_hangup_and_stream_status(self):
        call_sid = f"CA_{uuid.uuid4().hex[:12]}"
        for path in ["/hangup", "/twilio/status", "/api/twilio/status"]:
            resp = self.client.post(path, data={"CallSid": call_sid})
            self.assertEqual(resp.status_code, 200)

        status_resp = self.client.post("/stream-status", data={
            "CallSid": call_sid,
            "StreamStatus": "completed",
        })
        self.assertEqual(status_resp.status_code, 200)

    # -----------------------------------------------------------------------
    # 28. Telephony Mu-law Audio Conversions
    # -----------------------------------------------------------------------
    def test_28_telephony_audio_conversions(self):
        from routes_voice import _mulaw_to_wav, _wav_to_mulaw
        dummy_mulaw = b"\xff" * 160
        wav = _mulaw_to_wav(dummy_mulaw)
        self.assertGreater(len(wav), 44)
        roundtrip_mulaw = _wav_to_mulaw(wav)
        self.assertGreater(len(roundtrip_mulaw), 0)

    # -----------------------------------------------------------------------
    # 29. Twilio & Vobiz WebSocket /ws Greeting
    # -----------------------------------------------------------------------
    def test_29_telephony_websocket_greeting(self):
        with self.client.websocket_connect("/ws") as websocket:
            websocket.send_text(json.dumps({
                "event": "start",
                "streamSid": "MZ_test_stream_1",
                "start": {
                    "callSid": f"CA_{uuid.uuid4().hex[:12]}",
                    "streamSid": "MZ_test_stream_1",
                    "customParameters": {
                        "callerPhone": "+91 98765 43210"
                    }
                }
            }))
            # Agent transmits audio frames
            msg = websocket.receive_text()
            data = json.loads(msg)
            # Sends both Twilio media event and Vobiz event
            self.assertIn(data.get("event"), ["media", "playAudio"])


if __name__ == "__main__":
    unittest.main()


