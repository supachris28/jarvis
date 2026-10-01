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
