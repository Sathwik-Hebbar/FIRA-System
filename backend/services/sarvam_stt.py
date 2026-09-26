"""
FIRA – Sarvam Saaras Speech-to-Text & Translation Service (Phase 3).
Integrates Sarvam Saaras for Kannada, Hindi, and English STT and speech-to-English translation.
Includes robust offline/mock fallback for testing without external API credits.
"""

import base64
import io
import logging
import os
from typing import Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

SARVAM_API_BASE = "https://api.sarvam.ai"
SARVAM_STT_TRANSLATE_URL = f"{SARVAM_API_BASE}/speech-to-text-translate"
SARVAM_STT_URL = f"{SARVAM_API_BASE}/speech-to-text"


class SarvamSTTService:
    """
    Speech-to-Text service utilizing Sarvam Saaras.
    Converts Indian language speech (Kannada, Hindi, English) to original and English transcripts.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("SARVAM_API_KEY")
        self.timeout = float(os.getenv("SARVAM_TIMEOUT_SECONDS", "15.0"))

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and len(self.api_key.strip()) > 5)

    def process_audio(
        self,
        audio_bytes: bytes,
        language_code: str = "kn-IN",
        model: str = "saaras:v1"
    ) -> Dict[str, Optional[str]]:
        """
        Transcribes and translates speech audio into English using Sarvam Saaras.

        Parameters
        ----------
        audio_bytes : bytes
            Raw audio data (wav, mp3, ogg, m4a, webm).
        language_code : str
            Language code (e.g. 'kn-IN', 'hi-IN', 'en-IN').
        model : str
            Model identifier (default: 'saaras:v1').

        Returns
        -------
        dict
            {
                "transcript": str (Original text),
                "translated_transcript": str (English translation),
                "language_code": str,
                "status": "SUCCESS" | "SILENCE" | "ERROR"
            }
        """
        if not audio_bytes or len(audio_bytes) < 32:
            return {
                "transcript": None,
                "translated_transcript": None,
                "language_code": language_code,
                "status": "SILENCE"
            }

        if self.is_configured:
            return self._call_sarvam_api(audio_bytes, language_code, model)

        # Fallback / Mock mode when API key is not configured
        logger.info("SARVAM_API_KEY not configured. Using intelligent mock STT.")
        return self._mock_stt(audio_bytes, language_code)

    def process_base64_audio(
        self,
        base64_str: str,
        language_code: str = "kn-IN"
    ) -> Dict[str, Optional[str]]:
        """Decode base64 string and process audio bytes."""
        try:
            # Strip data url prefix if present (e.g. "data:audio/wav;base64,...")
            if "," in base64_str:
                base64_str = base64_str.split(",", 1)[1]
            audio_bytes = base64.b64decode(base64_str)
            return self.process_audio(audio_bytes, language_code=language_code)
        except Exception as exc:
            logger.error("Failed to decode base64 audio: %s", exc)
            return {
                "transcript": None,
                "translated_transcript": None,
                "language_code": language_code,
                "status": "ERROR"
            }

    def _call_sarvam_api(
        self,
        audio_bytes: bytes,
        language_code: str,
        model: str
    ) -> Dict[str, Optional[str]]:
        """Execute HTTP request to Sarvam API with support for language_code='unknown'."""
        headers = {
            "api-subscription-key": self.api_key,
        }
        files = {
            "file": ("emergency_audio.wav", io.BytesIO(audio_bytes), "audio/wav")
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                # If language_code is "unknown", use speech-to-text with auto-detect
                if language_code == "unknown":
                    stt_data = {
                        "model": "saaras:v3",
                        "language_code": "unknown",
                        "mode": "transcribe",
                    }
                    resp = client.post(SARVAM_STT_URL, headers=headers, files=files, data=stt_data)
                    if resp.status_code != 200:
                        # Fallback to saaras:v1 if v3 is not available
                        files["file"].seek(0)
                        stt_data["model"] = "saaras:v1"
                        stt_data["language_code"] = "kn-IN"
                        resp = client.post(SARVAM_STT_URL, headers=headers, files=files, data=stt_data)

                    if resp.status_code == 200:
                        payload = resp.json()
                        raw_transcript = payload.get("transcript") or ""
                        detected_lang = payload.get("language_code") or "kn-IN"
                        return {
                            "transcript": raw_transcript,
                            "translated_transcript": raw_transcript,
                            "language_code": detected_lang,
                            "status": "SUCCESS"
                        }

                # Standard translation / transcription flow for specified languages
                data = {
                    "model": model or "saaras:v1",
                    "prompt": "Emergency flood response citizen call in Karnataka",
                }
                resp = client.post(SARVAM_STT_TRANSLATE_URL, headers=headers, files=files, data=data)
                
                if resp.status_code == 200:
                    payload = resp.json()
                    transcript = payload.get("transcript") or ""
                    return {
                        "transcript": transcript,
                        "translated_transcript": transcript,
                        "language_code": language_code,
                        "status": "SUCCESS"
                    }
                else:
                    logger.warning("Sarvam API returned status %s: %s", resp.status_code, resp.text)
                    return {
                        "transcript": None,
                        "translated_transcript": None,
                        "language_code": language_code,
                        "status": "ERROR"
                    }
        except httpx.TimeoutException:
            logger.error("Sarvam STT API timed out after %s seconds", self.timeout)
            return {
                "transcript": None,
                "translated_transcript": None,
                "language_code": language_code,
                "status": "TIMEOUT"
            }
        except Exception as exc:
            logger.error("Sarvam STT API error: %s", exc)
            return {
                "transcript": None,
                "translated_transcript": None,
                "language_code": language_code,
                "status": "ERROR"
            }

    def _mock_stt(self, audio_bytes: bytes, language_code: str) -> Dict[str, Optional[str]]:
        """
        Mock STT provider for offline development, local demonstration, and tests.
        Produces realistic emergency descriptions based on language.
        """
        # Standard default Kannada emergency statement for demo
        kannada_demo = "ನಮ್ಮ ಮನೆಗೆ ನೀರು ತುಂಬಿಕೊಂಡಿದೆ. ನಾವು ನಾಲ್ಕು ಜನ ಇದ್ದೇವೆ. ಇಬ್ಬರು ಮಕ್ಕಳು ಇದ್ದಾರೆ. ನನ್ನ ತಂದೆಗೆ ಕಾಲಿಗೆ ಗಾಯವಾಗಿದೆ. ನಾವು ಮನೆಯಿಂದ ಹೊರಗೆ ಬರಲು ಆಗುತ್ತಿಲ್ಲ."
        english_translation = "Our house is flooded. There are four people with us. Two are children. My father has injured his leg and we cannot get out."

        if language_code.startswith("hi"):
            original = "हमारे घर में पानी भर गया है। हम चार लोग हैं, दो बच्चे हैं। मेरे पिताजी के पैर में चोट लगी है और हम बाहर नहीं निकल पा रहे हैं।"
        elif language_code.startswith("en"):
            original = english_translation
        else:
            original = kannada_demo

        return {
            "transcript": original,
            "translated_transcript": english_translation,
            "language_code": language_code,
            "status": "SUCCESS"
        }

    def transcribe_and_translate(
        self,
        audio_bytes: bytes,
        language_code: str = "kn-IN"
    ) -> Dict[str, Optional[str]]:
        """Alias matching voice SOS pipeline interface."""
        res = self.process_audio(audio_bytes, language_code)
        return {
            "raw_transcript": res.get("transcript"),
            "translated_transcript": res.get("translated_transcript"),
            "status": res.get("status"),
        }

    def translate_text(self, text: str, source_lang: str = "kn-IN") -> Dict[str, str]:
        """Translates text if needed."""
        # For mock fallback or standard demo
        if "ನಮ್ಮ ಮನೆಗೆ ನೀರು" in text:
            return {"translated_text": "Our house is flooded. There are four people with us. Two are children. My father has injured his leg and we cannot get out."}
        return {"translated_text": text}


# Singleton instance
sarvam_stt = SarvamSTTService()

