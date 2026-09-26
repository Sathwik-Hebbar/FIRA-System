"""
FIRA – Sarvam Bulbul Text-To-Speech (TTS) Service.
Converts emergency agent text responses into speech, primarily in Kannada (kn-IN),
as well as English (en-IN) and Hindi (hi-IN).

Isolates provider-specific logic and includes a clean offline/mock fallback
when SARVAM_API_KEY is not configured.
"""

import base64
import io
import logging
import os
from typing import Any, Dict, Optional
import wave
import httpx

logger = logging.getLogger("fira.tts")

SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")
SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"

# Standard speaker choices for Bulbul
LANGUAGE_SPEAKER_MAP = {
    "kn-IN": "meera",
    "en-IN": "amrita",
    "hi-IN": "aditi",
}

# Standard emergency confirmations
STANDARD_CONFIRMATIONS = {
    "kn-IN": "ನಿಮ್ಮ ಮಾಹಿತಿಯನ್ನು ದಾಖಲಿಸಲಾಗಿದೆ. ನಿಮ್ಮ ತುರ್ತು ವರದಿಯನ್ನು ರಕ್ಷಣಾ ತಂಡಕ್ಕೆ ಕಳುಹಿಸಲಾಗಿದೆ. ಸುರಕ್ಷಿತ ಸ್ಥಳದಲ್ಲಿರಿ, ಸಹಾಯ ಬರುತ್ತಿದೆ.",
    "en-IN": "Your emergency information has been recorded and dispatched to the rescue team. Please stay safe, help is on the way.",
    "hi-IN": "आपकी आपातकालीन जानकारी दर्ज कर ली गई है और बचाव दल को भेज दी गई है। कृपया सुरक्षित रहें, सहायता आ रही है।",
}


class SarvamTTSService:
    """Service to generate speech audio from text using Sarvam Bulbul API."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("SARVAM_API_KEY")

    def synthesize_speech(
        self,
        text: str,
        language: str = "kn-IN",
        speaker: Optional[str] = None,
        sample_rate: int = 16000,
    ) -> Dict[str, Any]:
        """
        Synthesizes speech from text.

        Returns a dictionary with:
        - "audio_base64": base64 encoded wav/mp3 string
        - "language": target language
        - "text": input text
        - "provider": "sarvam" or "mock"
        """
        if not text or not text.strip():
            return {
                "audio_base64": "",
                "language": language,
                "text": text,
                "provider": "empty",
            }

        # Normalize language
        lang_code = language if "-" in language else f"{language}-IN"
        env_speaker = os.getenv("TTS_SPEAKER")
        selected_speaker = speaker or env_speaker or LANGUAGE_SPEAKER_MAP.get(lang_code, "meera")

        if not self.api_key or self.api_key == "mock_key":
            logger.info("SARVAM_API_KEY not set. Using offline mock audio synthesis.")
            return self._mock_synthesize(text, lang_code)

        try:
            headers = {
                "api-subscription-key": self.api_key,
                "Content-Type": "application/json",
            }
            payload = {
                "inputs": [text.strip()],
                "target_language_code": lang_code,
                "speaker": selected_speaker,
                "pitch": 0,
                "pace": 1.0,
                "loudness": 1.5,
                "speech_sample_rate": sample_rate,
                "enable_preprocessing": True,
                "model": "bulbul:v1",
            }

            with httpx.Client(timeout=15.0) as client:
                response = client.post(SARVAM_TTS_URL, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                audios = data.get("audios", [])
                audio_base64 = audios[0] if audios else ""

                return {
                    "audio_base64": audio_base64,
                    "language": lang_code,
                    "text": text,
                    "provider": "sarvam",
                }

        except Exception as exc:
            logger.warning("Sarvam TTS request failed (%s). Falling back to mock synthesis.", exc)
            return self._mock_synthesize(text, lang_code)

    def _mock_synthesize(self, text: str, language: str) -> Dict[str, Any]:
        """Generates a valid silent PCM WAV with audio frames so downstream telephony decoders work."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(b"\x00" * 320)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return {
            "audio_base64": b64,
            "language": language,
            "text": text,
            "provider": "mock",
        }


# Singleton instance
sarvam_tts = SarvamTTSService()
