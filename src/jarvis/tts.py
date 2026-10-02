"""Text-to-speech for spoken replies.

Providers:
- kokoro     — Kokoro-82M via the Kokoro-FastAPI container on the server (CPU, local, recommended)
- elevenlabs — ElevenLabs cloud voices (sends reply text to ElevenLabs)
- browser    — no server TTS; the web app uses the device's built-in speech voices
"""

from __future__ import annotations

import re

import httpx

from . import http

from .config import Settings

MAX_CHARS = 1500


class SpeechError(Exception):
    """A user-facing speech error."""


def speech_text(text: str) -> str:
    """Strip Markdown, links and noise so replies sound natural."""
    text = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.S)
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", lambda m: m.group(1).rsplit("/", 1)[-1], text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.M)
    text = re.sub(r"[*_`~>|]", "", text)
    text = text.replace("→", " to ").replace("–", " to ").replace("—", ", ")
    return re.sub(r"\s+", " ", text).strip()


class Speech:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def provider(self) -> str:
        provider = self.settings.tts_provider
        if provider == "elevenlabs" and not (self.settings.elevenlabs_api_key and self.settings.elevenlabs_voice_id):
            return "browser"
        return provider if provider in {"kokoro", "elevenlabs"} else "browser"

    def status(self) -> dict:
        provider = self.provider
        if provider == "kokoro":
            return {"ok": True, "detail": f"Kokoro ({self.settings.tts_voice})", "provider": provider}
        if provider == "elevenlabs":
            return {"ok": True, "detail": "ElevenLabs (cloud)", "provider": provider}
        return {"ok": True, "detail": "browser voice (set JARVIS_TTS_PROVIDER=kokoro for Kokoro)",
                "provider": "browser"}

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        spoken = speech_text(text)[:MAX_CHARS]
        if not spoken:
            raise SpeechError("Nothing to say.")
        provider = self.provider
        try:
            client = http.shared(timeout=60)
            if provider == "kokoro":
                response = await client.post(
                    self.settings.tts_url.rstrip("/") + "/v1/audio/speech",
                    json={"model": "kokoro", "input": spoken, "voice": self.settings.tts_voice,
                          "response_format": "mp3", "speed": self.settings.tts_speed},
                )
            elif provider == "elevenlabs":
                response = await client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{self.settings.elevenlabs_voice_id}",
                    params={"output_format": "mp3_44100_128"},
                    headers={"xi-api-key": self.settings.elevenlabs_api_key, "Accept": "audio/mpeg"},
                    json={"text": spoken, "model_id": self.settings.elevenlabs_model_id},
                )
            else:
                raise SpeechError("Server speech is not configured; the browser voice is used instead.")
        except httpx.HTTPError as error:
            raise SpeechError(f"Speech service unreachable ({type(error).__name__}).") from None
        if response.status_code >= 400:
            raise SpeechError(f"Speech service returned HTTP {response.status_code}: {response.text[:200]}")
        return response.content, response.headers.get("content-type", "audio/mpeg").split(";")[0]


class Hearing:
    """Speech to text for the 🎤 button: a Whisper server on the PC (OpenAI-compatible
    /v1/audio/transcriptions, e.g. speaches / faster-whisper-server) when JARVIS_STT_URL is set and reachable;
    otherwise the app uses the phone's own speech recognition."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.stt_url)

    async def health(self) -> dict:
        if not self.configured:
            return {"ok": True, "detail": "phone/browser speech recognition (set JARVIS_STT_URL for Whisper)"}
        try:
            response = await http.shared(timeout=5).get(self.settings.stt_url.rstrip("/") + "/health")
        except httpx.HTTPError as error:
            return {"ok": False, "detail": f"Whisper offline ({type(error).__name__}) — using phone recognition"}
        return {"ok": response.status_code < 400, "detail": f"Whisper on the PC ({self.settings.stt_model})"
                if response.status_code < 400 else f"Whisper HTTP {response.status_code}"}

    async def transcribe(self, audio: bytes, content_type: str) -> str:
        if not self.configured:
            raise SpeechError("Server speech recognition isn't set up.")
        extension = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/mpeg": "mp3",
                     "audio/wav": "wav", "audio/x-wav": "wav"}.get(content_type.split(";")[0].strip(), "webm")
        try:
            response = await http.shared(timeout=60).post(
                self.settings.stt_url.rstrip("/") + "/v1/audio/transcriptions",
                files={"file": (f"speech.{extension}", audio, content_type or "audio/webm")},
                data={"model": self.settings.stt_model, "language": "en", "response_format": "json"})
        except httpx.HTTPError as error:
            raise SpeechError(f"Whisper unreachable ({type(error).__name__}).") from None
        if response.status_code >= 400:
            raise SpeechError(f"Whisper returned HTTP {response.status_code}: {response.text[:200]}")
        try:
            return str(response.json().get("text", "")).strip()
        except ValueError:
            return response.text.strip()
