"""
FIRA – Conversation State Machine (Phase 5).
Maintains a controlled, multi-turn emergency conversation flow.
Collects missing vital information without repeating already-answered questions.
Provides natural prompts in Kannada, English, and Hindi.
"""

from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
import uuid

import sys
from pathlib import Path
_backend_dir = str(Path(__file__).resolve().parent.parent)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from schemas.voice_sos import EmergencyExtraction


# ---------------------------------------------------------------------------
# Multilingual Question Bank & Helpline Protocols
# ---------------------------------------------------------------------------

OPENING_GREETINGS = {
    "kn": "ನಮಸ್ಕಾರ, ಇದು ಪ್ರವಾಹ ತುರ್ತು ಸಹಾಯವಾಣಿ. ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?",
    "en": "Namaskara, how can I help you?",
    "hi": "नमस्ते, यह बाढ़ आपातकालीन हेल्पलाइन है। मैं आपकी कैसे मदद कर सकता हूँ?",
}

QUESTIONS = [
    {
        "key": "safe_and_emergency",
        "kn": "ನೀವು ಮತ್ತು ನಿಮ್ಮ ಕುಟುಂಬ ಈಗ ಸುರಕ್ಷಿತವಾಗಿದ್ದೀರಾ? ನೀರಿನ ಮಟ್ಟ ಎಷ್ಟಿದೆ?",
        "en": "Are you and your family safe right now? What is the water level around you?",
        "hi": "क्या आप और आपका परिवार अभी सुरक्षित हैं? आपके आसपास पानी कितना है?",
    },
    {
        "key": "location",
        "kn": "ನೀವು ಈಗ ಎಲ್ಲಿದ್ದೀರಿ? ನಿಮ್ಮ ಗ್ರಾಮ ಅಥವಾ ಹತ್ತಿರದ ಪ್ರಮುಖ ಸ್ಥಳ ತಿಳಿಸಿ.",
        "en": "Where are you now? Please state your village, area, or a nearby landmark.",
        "hi": "आप अभी कहाँ हैं? कृपया अपना गाँव, इलाका या कोई नजदीकी पहचान बताएं।",
    },
    {
        "key": "people_and_trapped",
        "kn": "ನಿಮ್ಮೊಂದಿಗೆ ಎಷ್ಟು ಜನ ಇದ್ದಾರೆ? ಯಾರಾದರೂ ಮನೆಯೊಳಗೆ ಸಿಲುಕಿಕೊಂಡಿದ್ದಾರಾ?",
        "en": "How many people are with you in danger? Is anyone trapped inside?",
        "hi": "आपके साथ कितने लोग हैं? क्या कोई अंदर फंसा हुआ है?",
    },
    {
        "key": "vulnerable",
        "kn": "ನಿಮ್ಮೊಂದಿಗೆ ಮಕ್ಕಳು, ವೃದ್ಧರು ಅಥವಾ ಗರ್ಭಿಣಿಯರು ಯಾರಾದರೂ ಇದ್ದಾರಾ?",
        "en": "Are there any children, elderly individuals, or pregnant women with you?",
        "hi": "क्या आपके साथ कोई बच्चे, बुजुर्ग या गर्भवती महिलाएं हैं?",
    },
    {
        "key": "medical_and_injured",
        "kn": "ಯಾರಿಗಾದರೂ ಗಾಯವಾಗಿದೆಯಾ ಅಥವಾ ತುರ್ತು ಚಿಕಿತ್ಸೆ ಬೇಕೇ?",
        "en": "Is anyone injured or in need of urgent medical treatment?",
        "hi": "क्या कोई घायल है या किसी को तत्काल इलाज की जरूरत है?",
    },
    {
        "key": "evacuation",
        "kn": "ನಿಮಗೆ ಸ್ವತಃ ಸುರಕ್ಷಿತ ಸ್ಥಳಕ್ಕೆ ಬರಲು ಸಾಧ್ಯವೇ, ಅಥವಾ ರಕ್ಷಣಾ ದೋಣಿ ಬೇಕೇ?",
        "en": "Can you move to safety on your own, or do you require a rescue boat?",
        "hi": "क्या आप खुद सुरक्षित स्थान पर जा सकते हैं, या आपको बचाव नाव चाहिए?",
    },
]

CONFIRMATION_MESSAGES = {
    "standard": {
        "kn": "ನಿಮ್ಮ ತುರ್ತು ವರದಿಯನ್ನು ದಾಖಲಿಸಲಾಗಿದೆ. ದಯವಿಟ್ಟು ಸುರಕ್ಷಿತವಾಗಿರಿ ಮತ್ತು ತುರ್ತು ರಕ್ಷಣಾ ತಂಡದ ಸೂಚನೆಗಳನ್ನು ಪಾಲಿಸಿ.",
        "en": "Your emergency has been registered. Please stay safe and follow the instructions of the emergency team.",
        "hi": "आपकी आपातकालीन स्थिति दर्ज कर ली गई है। कृपया सुरक्षित रहें और बचाव दल के निर्देशों का पालन करें।",
    },
    "critical": {
        "kn": "ನಿಮ್ಮ ವರದಿಯನ್ನು ಗಂಭೀರ ತುರ್ತು ಎಂದು ಗುರುತಿಸಿ ರಕ್ಷಣಾ ತಂಡಕ್ಕೆ ಕಳುಹಿಸಲಾಗಿದೆ. ಧೈರ್ಯವಾಗಿರಿ, ರಕ್ಷಕರು ತಲುಪುತ್ತಿದ್ದಾರೆ.",
        "en": "Your emergency has been marked as critical and sent to the response team. Please stay safe.",
        "hi": "आपकी स्थिति को अत्यंत गंभीर मानकर बचाव दल को भेज दिया गया है। कृपया सुरक्षित रहें, सहायता भेजी जा रही है।",
    },
}

SAFETY_INSTRUCTIONS = {
    "kn": "ಸೂಚನೆ: ಸುರಕ್ಷಿತವಾದ ಎತ್ತರದ ಸ್ಥಳಕ್ಕೆ ತೆರಳಿ. ವೇಗವಾಗಿ ಹರಿಯುವ ನೀರಿಗೆ ಇಳಿಯಬೇಡಿ ಮತ್ತು ವಿದ್ಯುತ್ ಕಂಬಗಳಿಂದ ದೂರವಿರಿ. ಮಕ್ಕಳ ಜೊತೆಯೇ ಇರಿ.",
    "en": "Safety instruction: Move to higher ground if safe to do so. Do not enter moving floodwater. Stay away from electrical poles and keep children close.",
    "hi": "सुरक्षा निर्देश: यदि सुरक्षित हो तो ऊंचे स्थान पर जाएं। बहते पानी में न उतरें। बिजली के खंभों से दूर रहें और बच्चों को साथ रखें।",
}

SILENCE_PROMPT = {
    "kn": "ನಿಮಗೆ ನನ್ನ ಧ್ವನಿ ಕೇಳಿಸುತ್ತಿದೆಯಾ? ನೀವು ಅಪಾಯದಲ್ಲಿದ್ದೀರಾ?",
    "en": "Can you hear me? Are you in danger?",
    "hi": "क्या आप मुझे सुन पा रहे हैं? क्या आप खतरे में हैं?",
}

UNCLEAR_PROMPT = {
    "kn": "ಮಾಹಿತಿ ಸ್ಪಷ್ಟವಾಗಿ ಅರ್ಥವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ನಿಮ್ಮ ಗ್ರಾಮ ಅಥವಾ ಹತ್ತಿರದ ಪ್ರಮುಖ ಸ್ಥಳವನ್ನು ಮತ್ತೊಮ್ಮೆ ತಿಳಿಸಿ.",
    "en": "I did not understand clearly. Please state your village or nearest landmark again.",
    "hi": "मुझे स्पष्ट रूप से समझ नहीं आया। कृपया अपना गाँव या नजदीकी पहचान फिर से बताएं।",
}


class VoiceSessionState:
    """Represents an active multi-turn emergency phone call session."""

    def __init__(
        self,
        session_id: Optional[str] = None,
        caller_phone: str = "",
        citizen_id: Optional[int] = None,
        language: str = "kn-IN"
    ):
        self.session_id = session_id or str(uuid.uuid4())
        self.caller_phone = caller_phone
        self.citizen_id = citizen_id
        self.language = language
        self.started_at = datetime.now(timezone.utc)
        self.ended_at: Optional[datetime] = None
        self.status = "VOICE_SESSION_STARTED"
        self.turns: List[Dict[str, str]] = []
        self.extracted_data: EmergencyExtraction = EmergencyExtraction()
        self.raw_transcripts: List[str] = []
        self.translated_transcripts: List[str] = []
        self.current_question_index = 0
        self.is_completed = False

    def update_from_extraction(self, new_data: EmergencyExtraction):
        """Merges new extracted facts, never overwriting existing truth with None."""
        current_dict = self.extracted_data.model_dump()
        new_dict = new_data.model_dump()

        for key, val in new_dict.items():
            if val is not None:
                current_dict[key] = val

        self.extracted_data = EmergencyExtraction(**current_dict)

    def get_next_question(self) -> Optional[str]:
        """
        Determines the next unanswered critical question.
        Skips questions whose answers are already known from earlier statements.
        Does not ask redundant questions if situation is already clearly critical.
        """
        lang = "kn" if self.language.startswith("kn") else ("hi" if self.language.startswith("hi") else "en")
        is_critical = bool(self.extracted_data.trapped or self.extracted_data.injured or self.extracted_data.medical_emergency)

        # 1. Location is always vital
        if not self.extracted_data.location_description:
            return QUESTIONS[1][lang]

        # 2. Number of people in danger
        if self.extracted_data.people_count is None:
            return QUESTIONS[2][lang]

        # 3. If not yet known if someone is injured or trapped
        if self.extracted_data.trapped is None and self.extracted_data.injured is None:
            return QUESTIONS[4][lang]

        # 4. Vulnerable individuals (children/elderly)
        if self.extracted_data.children_count is None and self.extracted_data.elderly_count is None and not is_critical:
            return QUESTIONS[3][lang]

        # All critical fields collected or emergency situation clear
        self.is_completed = True
        conf_type = "critical" if is_critical else "standard"
        conf_msg = CONFIRMATION_MESSAGES[conf_type].get(lang, CONFIRMATION_MESSAGES[conf_type]["kn"])
        safety = SAFETY_INSTRUCTIONS.get(lang, SAFETY_INSTRUCTIONS["kn"])
        return f"{conf_msg} {safety}"


# In-memory session registry for active calls
ACTIVE_SESSIONS: Dict[str, VoiceSessionState] = {}


def get_or_create_session(
    session_id: Optional[str] = None,
    caller_phone: str = "",
    citizen_id: Optional[int] = None,
    language: str = "kn-IN"
) -> VoiceSessionState:
    """Retrieve existing session by ID or create a new session."""
    if session_id and session_id in ACTIVE_SESSIONS:
        return ACTIVE_SESSIONS[session_id]

    state = VoiceSessionState(
        session_id=session_id,
        caller_phone=caller_phone,
        citizen_id=citizen_id,
        language=language
    )
    ACTIVE_SESSIONS[state.session_id] = state
    return state
