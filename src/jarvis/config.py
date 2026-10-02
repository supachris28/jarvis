"""Settings loaded from environment variables (see .env.server.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    public_url: str = "http://localhost:8080"
    data_dir: Path = Path("/data")
    timezone: str = "Europe/London"
    secure_cookies: bool = True

    ollama_url: str = "http://localhost:11434"
    chat_model: str = "llama3.2:3b"
    router_model: str = ""
    llm_timeout: float = 180.0
    bible_translation: str = "webbe"   # bible-api.com id: webbe (World English Bible, British), web, kjv, oeb-cw…
    ollama_keep_alive: str = "-1"      # how long Ollama keeps the model in GPU memory: -1 = until Ollama stops

    vault_backend: str = "files"          # files (folder on the server) | obsidian (Local REST API plugin)
    vault_path: Path = Path("/vault")
    vault_file_mode: int = 0o664
    vault_write: str = "nextcloud"        # nextcloud (WebDAV, Nextcloud sees changes at once) | disk
    nextcloud_url: str = ""
    nextcloud_user: str = ""
    nextcloud_app_password: str = ""
    nextcloud_vault_dir: str = "Obsidian/Jarvis"
    nextcloud_verify_tls: bool = True
    obsidian_url: str = "https://127.0.0.1:27124"
    obsidian_api_key: str = ""
    obsidian_ca_cert: str = ""
    obsidian_verify_tls: bool = True
    obsidian_vault_name: str = "Jarvis"

    google_client_id: str = ""
    google_client_secret: str = ""
    google_calendar_ids: list[str] = field(default_factory=lambda: ["primary"])

    ntfy_url: str = ""
    ntfy_topic: str = "jarvis"
    ntfy_token: str = ""
    notify_vip_senders: list[str] = field(default_factory=list)
    notify_keywords: list[str] = field(default_factory=list)
    notify_quiet_hours: str = "22:00-07:00"
    notify_event_lead_minutes: int = 60
    notify_max_per_hour: int = 10

    events_llm_daily_limit: int = 150
    events_auto_add: str = "off"          # off | ics
    events_calendar_id: str = "primary"

    vault_save_notify: str = "summary"    # summary | off
    vault_save_notify_minutes: int = 15

    tts_provider: str = "browser"         # kokoro | elevenlabs | browser
    tts_url: str = "http://kokoro:8880"
    tts_voice: str = "bm_george"
    tts_speed: float = 1.0
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model_id: str = "eleven_flash_v2_5"

    ha_url: str = ""
    ha_token: str = ""
    ha_verify_tls: bool = True

    brief_time: str = "07:30"
    backup_time: str = "03:15"           # nightly database backup; "off" to disable
    backup_dir: str = "Backups/Jarvis"   # Nextcloud folder for backups (never inside the vault)
    evening_time: str = "21:00"         # evening preview of tomorrow; "" or "off" to switch it off
    brief_latitude: str = ""
    brief_longitude: str = ""
    brief_place: str = ""
    brief_ha_entities: list[str] = field(default_factory=list)

    web_search_provider: str = "searxng"   # searxng | brave | off
    searxng_url: str = "http://searxng:8080"
    brave_api_key: str = ""
    web_fetch_pages: int = 3
    web_cache_minutes: int = 60
    web_language: str = "en-GB"
    web_allow_private: bool = False

    log_level: str = "info"                # info | debug (verbose can also be switched on from the UI)
    log_retention_days: int = 7

    gmail_interval: int = 300
    calendar_interval: int = 900
    backfill_days: int = 30
    email_body_limit: int = 8000

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jarvis.sqlite3"

    @property
    def google_token_path(self) -> Path:
        return self.data_dir / "google-token.json"

    @property
    def google_redirect_uri(self) -> str:
        return self.public_url.rstrip("/") + "/auth/google/callback"

    @property
    def effective_router_model(self) -> str:
        return self.router_model or self.chat_model

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            public_url=_str("JARVIS_PUBLIC_URL", "http://localhost:8080"),
            data_dir=Path(_str("JARVIS_DATA_DIR", "/data")),
            timezone=_str("JARVIS_TIMEZONE", "Europe/London"),
            secure_cookies=_bool("JARVIS_SECURE_COOKIES", True),
            ollama_url=_str("JARVIS_OLLAMA_URL", "http://localhost:11434"),
            chat_model=_str("JARVIS_MODEL", "llama3.2:3b"),
            router_model=_str("JARVIS_ROUTER_MODEL"),
            llm_timeout=float(_int("JARVIS_LLM_TIMEOUT", 180)),
            ollama_keep_alive=_str("JARVIS_OLLAMA_KEEP_ALIVE", "-1"),
            bible_translation=_str("JARVIS_BIBLE_TRANSLATION", "webbe"),
            vault_backend=_str("VAULT_BACKEND", "files").casefold(),
            vault_path=Path(_str("VAULT_PATH", "/vault")),
            vault_file_mode=int(_str("VAULT_FILE_MODE", "664") or "664", 8),
            vault_write=_str("VAULT_WRITE", "nextcloud").casefold(),
            nextcloud_url=_str("NEXTCLOUD_URL"),
            nextcloud_user=_str("NEXTCLOUD_USER"),
            nextcloud_app_password=_str("NEXTCLOUD_APP_PASSWORD"),
            nextcloud_vault_dir=_str("NEXTCLOUD_VAULT_DIR", "Obsidian/Jarvis"),
            nextcloud_verify_tls=_bool("NEXTCLOUD_VERIFY_TLS", True),
            obsidian_url=_str("OBSIDIAN_URL", "https://127.0.0.1:27124"),
            obsidian_api_key=_str("OBSIDIAN_API_KEY"),
            obsidian_ca_cert=_str("OBSIDIAN_CA_CERT"),
            obsidian_verify_tls=_bool("OBSIDIAN_VERIFY_TLS", True),
            obsidian_vault_name=_str("OBSIDIAN_VAULT_NAME", "Jarvis"),
            google_client_id=_str("GOOGLE_CLIENT_ID"),
            google_client_secret=_str("GOOGLE_CLIENT_SECRET"),
            google_calendar_ids=_list("GOOGLE_CALENDAR_IDS") or ["primary"],
            ntfy_url=_str("NTFY_URL"),
            ntfy_topic=_str("NTFY_TOPIC", "jarvis"),
            ntfy_token=_str("NTFY_TOKEN"),
            notify_vip_senders=[s.casefold() for s in _list("NOTIFY_VIP_SENDERS")],
            notify_keywords=[s.casefold() for s in _list("NOTIFY_KEYWORDS")],
            notify_quiet_hours=_str("NOTIFY_QUIET_HOURS", "22:00-07:00"),
            notify_event_lead_minutes=_int("NOTIFY_EVENT_LEAD_MINUTES", 60),
            notify_max_per_hour=_int("NOTIFY_MAX_PER_HOUR", 10),
            events_llm_daily_limit=_int("EVENTS_LLM_DAILY_LIMIT", 150),
            events_auto_add=_str("EVENTS_AUTO_ADD", "off").casefold(),
            events_calendar_id=_str("EVENTS_CALENDAR_ID", "primary"),
            vault_save_notify=_str("NOTIFY_VAULT_SAVES", "summary").casefold(),
            vault_save_notify_minutes=_int("NOTIFY_VAULT_SAVES_MINUTES", 15),
            tts_provider=_str("JARVIS_TTS_PROVIDER", "browser").casefold(),
            tts_url=_str("JARVIS_TTS_URL", "http://kokoro:8880"),
            tts_voice=_str("JARVIS_TTS_VOICE", "bm_george"),
            tts_speed=float(_str("JARVIS_TTS_SPEED", "1.0") or 1.0),
            elevenlabs_api_key=_str("ELEVENLABS_API_KEY"),
            elevenlabs_voice_id=_str("ELEVENLABS_VOICE_ID"),
            elevenlabs_model_id=_str("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5"),
            ha_url=_str("HA_URL"),
            ha_token=_str("HA_TOKEN"),
            ha_verify_tls=_bool("HA_VERIFY_TLS", True),
            brief_time=_str("BRIEF_TIME", "07:30"),
            evening_time=_str("EVENING_TIME", "21:00"),
            backup_time=_str("BACKUP_TIME", "03:15"),
            backup_dir=_str("BACKUP_DIR", "Backups/Jarvis"),
            brief_latitude=_str("BRIEF_LATITUDE"),
            brief_longitude=_str("BRIEF_LONGITUDE"),
            brief_place=_str("BRIEF_PLACE"),
            brief_ha_entities=_list("BRIEF_HA_ENTITIES"),
            web_search_provider=_str("WEB_SEARCH_PROVIDER", "searxng").casefold(),
            searxng_url=_str("SEARXNG_URL", "http://searxng:8080"),
            brave_api_key=_str("BRAVE_API_KEY"),
            web_fetch_pages=_int("WEB_FETCH_PAGES", 3),
            web_cache_minutes=_int("WEB_CACHE_MINUTES", 60),
            web_language=_str("WEB_LANGUAGE", "en-GB"),
            web_allow_private=_bool("WEB_ALLOW_PRIVATE", False),
            log_level=_str("JARVIS_LOG_LEVEL", "info").casefold(),
            log_retention_days=_int("JARVIS_LOG_RETENTION_DAYS", 7),
            gmail_interval=_int("INGEST_GMAIL_INTERVAL", 300),
            calendar_interval=_int("INGEST_CALENDAR_INTERVAL", 900),
            backfill_days=_int("INGEST_BACKFILL_DAYS", 30),
            email_body_limit=_int("INGEST_EMAIL_BODY_LIMIT", 8000),
        )
