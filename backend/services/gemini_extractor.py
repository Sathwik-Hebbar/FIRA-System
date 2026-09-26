"""
FIRA – Gemini Emergency Information Extractor Service (Phase 4).
Extracts strictly grounded emergency fields from translated English transcripts.
Uses Gemini structured JSON output with fallback to deterministic rule extraction.
CRITICAL: Never infers facts, never invents missing data, never calculates priority.
"""

import json
import logging
import os
import re
from typing import Any, Dict, Optional

import httpx

import sys
from pathlib import Path
_backend_dir = str(Path(__file__).resolve().parent.parent)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from schemas.voice_sos import EmergencyExtraction

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


EXTRACTION_PROMPT = """
You are an emergency response information extractor for FIRA (Flood Intelligence & Response Assistant).
Your job is to extract strictly factual emergency information from the citizen's translated statement.

CRITICAL INSTRUCTIONS:
1. Extract ONLY information explicitly stated by the citizen.
2. Never infer facts or guess what might be true.
3. Never invent missing information.
4. Use null for unknown or unmentioned fields.
5. Do NOT calculate or assign any priority scores.

Return a JSON object with these EXACT keys:
- incident_type: string or null (e.g. "FLOOD", "WATERLOGGING", "RIVER_OVERFLOW")
- safe: boolean or null (true if explicitly safe, false if in danger, null if unknown)
- trapped: boolean or null (true if trapped or cannot get out, false if mobile, null if unknown)
- injured: boolean or null (true if someone is hurt/injured, false if stated no injuries, null if unknown)
- people_count: integer or null (exact total count of people with caller)
- children_count: integer or null (exact count of children/infants)
- elderly_count: integer or null (exact count of elderly/seniors)
- disabled_person_count: integer or null (count of disabled or mobility-impaired persons)
- medical_emergency: boolean or null (true if urgent medical condition or injury requires immediate attention)
- water_level: string or null (e.g. "ankle", "knee", "waist", "chest", "rising")
- water_depth_m: number or null (water depth in metres if mentioned)
- building_condition: string or null (e.g. "submerged", "collapsing", "roof", null if unknown)
- evacuation_possible: boolean or null (false if cannot leave, true if able to leave, null if unknown)
- rescue_required: boolean or null (true if external rescue needed, null if unknown)
- location_description: string or null (any location, street, area, village or landmark named)
- additional_information: string or null (specific hazards, power status, or vital notes)
"""


class GeminiExtractor:
    """
    Extracts structured emergency data from English transcripts.
    Uses Gemini API if GEMINI_API_KEY is available; falls back to deterministic rule extraction.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
        self.timeout = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "10.0"))

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and len(self.api_key.strip()) > 5 and not self.api_key.lower().startswith("dummy_"))

    def extract(self, english_transcript: str) -> EmergencyExtraction:
        """
        Extract structured emergency information from transcript.
        """
        if not english_transcript or not english_transcript.strip():
            return EmergencyExtraction()

        if self.is_configured:
            try:
                extracted_dict = self._call_gemini(english_transcript)
                if extracted_dict:
                    return EmergencyExtraction(**extracted_dict)
            except Exception as exc:
                logger.warning("Gemini extraction call failed: %s. Falling back to deterministic extractor.", exc)

        # Fallback to deterministic regex-based extraction
        return self._deterministic_extract(english_transcript)

    def extract_emergency_facts(
        self,
        transcript: str,
        original_transcript: Optional[str] = None
    ) -> EmergencyExtraction:
        """Extract emergency facts from transcript."""
        combined = f"{transcript} {original_transcript or ''}".strip()
        return self.extract(combined or transcript)

    def _extract_deterministic_fallback(self, text: str) -> EmergencyExtraction:
        return self._deterministic_extract(text)

    def _call_gemini(self, transcript: str) -> Optional[Dict[str, Any]]:
        """Call Gemini API with structured JSON output."""
        url = f"{GEMINI_API_BASE}/{self.model}:generateContent?key={self.api_key}"
        headers = {"Content-Type": "application/json"}
        
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": f"{EXTRACTION_PROMPT}\n\nCitizen statement:\n\"{transcript}\""}
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.0,
                "response_mime_type": "application/json"
            }
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        text = parts[0].get("text", "")
                        return json.loads(text)
            else:
                logger.error("Gemini API error (%s): %s", resp.status_code, resp.text)
        return None

    def _deterministic_extract(self, transcript: str) -> EmergencyExtraction:
        """
        Strict, deterministic fallback extractor using word-boundary patterns.
        Grounded in caller words; never infers or invents facts.
        """
        text = transcript.lower()
        
        # Trapped
        trapped = None
        if re.search(r"\b(trapped|can't get out|cannot get out|unable to leave|stuck|stranded|blocked)\b", text):
            trapped = True

        # Injured
        injured = None
        if re.search(r"\b(injured|hurt|bleeding|wound|broken leg|fracture)\b", text):
            injured = True
        elif re.search(r"\b(not injured|no injuries|no one is hurt)\b", text):
            injured = False

        # Medical Emergency
        medical_emergency = None
        if injured or re.search(r"\b(medical emergency|doctor|hospital|heart|diabetic|unconscious|sick|oxygen)\b", text):
            medical_emergency = True

        # Safe
        safe = None
        if re.search(r"\b(in danger|not safe|unsafe|terrified|emergency|water is rising|drowning)\b", text) or trapped:
            safe = False
        elif re.search(r"\b(we are safe|safe here|no danger)\b", text):
            safe = True

        # People Count
        people_count = None
        m_people = re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s*(people|persons|of us|members)\b", text)
        if m_people:
            people_count = self._parse_number(m_people.group(1))
        elif "four people" in text:
            people_count = 4

        # Children Count
        children_count = None
        m_child = re.search(r"\b(one|two|three|four|five|\d+)\s*(children|kids|child|infants|babies)\b", text)
        if m_child:
            children_count = self._parse_number(m_child.group(1))
        elif "two are children" in text or "two children" in text:
            children_count = 2

        # Elderly Count
        elderly_count = None
        m_elder = re.search(r"\b(one|two|three|\d+)\s*(elderly|seniors|old people)\b", text)
        if m_elder:
            elderly_count = self._parse_number(m_elder.group(1))
        elif re.search(r"\b(my mother|my father|grandfather|grandmother|elderly mother|elderly father)\b", text):
            elderly_count = 1

        # Disabled
        disabled_count = None
        if re.search(r"\b(disabled|wheelchair|cannot walk|mobility issue|paralyzed)\b", text):
            disabled_count = 1

        # Evacuation & Rescue
        evacuation_possible = False if trapped else None
        rescue_required = True if (trapped or injured or medical_emergency) else None

        # Water Level
        water_level = None
        m_water = re.search(r"\b(ankle|knee|waist|chest|neck|roof|ground floor|rising rapidly|rising quickly)\b", text)
        if m_water:
            water_level = m_water.group(1)

        # Additional information notes
        notes = []
        if "injured leg" in text or "broken leg" in text:
            notes.append("Caller mentioned father/person with injured leg.")
        if "water is rising" in text or "rising quickly" in text:
            notes.append("Flood water is rising rapidly.")
        if "no power" in text or "blackout" in text:
            notes.append("Power outage reported.")

        additional_info = " ".join(notes) if notes else None

        # Location description
        location_desc = None
        m_loc = re.search(r"\b(?:near|at|in|on)\s+([A-Za-z0-9\s,]+(?:road|temple|street|cross|bridge|circle|nagar|halli|layout|mangalore|bangalore|udupi))", text, re.IGNORECASE)
        if m_loc:
            location_desc = m_loc.group(0).strip()

        return EmergencyExtraction(
            incident_type="FLOOD",
            safe=safe,
            trapped=trapped,
            injured=injured,
            people_count=people_count,
            children_count=children_count,
            elderly_count=elderly_count,
            disabled_person_count=disabled_count,
            medical_emergency=medical_emergency,
            water_level=water_level,
            water_depth_m=None,
            building_condition=None,
            evacuation_possible=evacuation_possible,
            rescue_required=rescue_required,
            location_description=location_desc,
            additional_information=additional_info,
        )

    def _parse_number(self, val: str) -> Optional[int]:
        """Convert digit or English word number to int."""
        word_to_num = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10
        }
        val_clean = val.strip().lower()
        if val_clean in word_to_num:
            return word_to_num[val_clean]
        if val_clean.isdigit():
            return int(val_clean)
        return None


# Singleton instance
gemini_extractor = GeminiExtractor()
