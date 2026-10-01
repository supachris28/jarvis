# Voice output

Jarvis can speak Ollama responses through either a local speech engine or ElevenLabs. Speech is disabled by default. Enable it with `JARVIS_VOICE=true`, `--voice`, or `:voice on` during a chat session.

## ElevenLabs

Add these settings to `.env`:

```env
JARVIS_TTS_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=your-api-key
ELEVENLABS_VOICE_ID=your-voice-id
ELEVENLABS_MODEL_ID=eleven_flash_v2_5
```

Then start Jarvis with `python3 jarvis.py --voice`. When both credentials are present, Jarvis automatically selects ElevenLabs unless `JARVIS_TTS_PROVIDER=local` is set explicitly. Jarvis sends cleaned response text to the ElevenLabs text-to-speech endpoint, writes the returned MP3 to a temporary file, plays it, and removes the file afterward. The API key is never printed or stored in the repository.

On Windows, Jarvis uses Windows Media Player through PowerShell for playback. On Linux, install `ffplay` or `mpv`. If no player is available, `:status` reports the missing dependency.

ElevenLabs is a cloud provider: enabled responses are sent to ElevenLabs and consume account usage. Use `JARVIS_TTS_PROVIDER=local` to keep speech synthesis on the machine.

## Controls

```text
:voice on
:voice off
:voice status
```

If speech fails, Jarvis reports the error and disables voice output for the rest of the session while keeping text chat available.
