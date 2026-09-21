"""
Saksham AI - Configuration Management
Secure, centralized configuration using Pydantic Settings
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Saksham AI Configuration
    All sensitive values loaded from environment variables
    """
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    # Application
    app_name: str = "Saksham AI"
    app_version: str = "0.1.0"
    debug: bool = False
    
    # API Server
    host: str = "127.0.0.1"
    port: int = 8420
    
    # Security
    api_key: SecretStr = Field(default=SecretStr(""), description="Internal API key for frontend-backend auth")
    # The Vite desktop shell is the only browser client by default.  Do not
    # use a wildcard here: the backend can initiate local system actions.
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    
    # LLM Provider Configuration (Generic)
    # Supports Cloud OpenAI or Local LM Studio (openai-compatible)
    llm_api_key: SecretStr = Field(default=SecretStr("lm-studio"), description="API Key (use 'lm-studio' or actual OpenAI key)")
    llm_base_url: str = Field(default="http://localhost:1234/v1", description="Base URL (e.g. http://localhost:1234/v1 for LM Studio)")
    llm_model: str = "openai/gpt-oss-20b"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 4096
    
    # Web Search (RAG)
    search_provider: Literal["google", "duckduckgo"] = "duckduckgo" # Default to DDG (Free)
    search_api_key: Optional[SecretStr] = None # Only needed for Google/Bing

    # Voice Configuration
    voice_enabled: bool = True
    whisper_model: str = "base"  # Options: tiny, base, small, medium, large
    voice_language: str = "en"
    voice_turn_settle_seconds: float = 0.85
    voice_incomplete_turn_settle_seconds: float = 2.0
    tts_enabled: bool = True
    tts_provider: Literal["chatterbox", "edge", "openai", "system", "piper"] = "edge"
    tts_voice: str = "onyx"  # OpenAI TTS voice
    edge_tts_voice: str = "en-US-AndrewMultilingualNeural"  # Warm conversational voice
    piper_model_path: str = "models/piper/en_US-lessac-medium.onnx" # Path to Piper model
    openai_tts_api_key: Optional[SecretStr] = Field(default=None, description="Separate OpenAI API key for TTS (if using LM Studio for chat)")
    chatterbox_tts_url: str = "http://127.0.0.1:8100"
    chatterbox_tts_api_key: Optional[SecretStr] = None
    chatterbox_tts_voice: str = "saksham"
    chatterbox_tts_timeout_seconds: float = 45.0
    chatterbox_tts_fallback: Literal["none", "edge", "piper", "openai", "system"] = "none"
    chatterbox_tts_autostart: bool = True
    chatterbox_tts_startup_timeout_seconds: float = 120.0

    # Meeting intelligence
    meeting_mode_enabled: bool = True
    projects_root: Path = Field(
        default=Path.home() / "Documents" / "Saksham Projects",
        description="Root directory containing Saksham-managed client projects",
    )
    meeting_audio_retention_days: int = 30
    meeting_sample_rate: int = 16000
    meeting_live_transcription_seconds: int = 3
    meeting_pointer_interval_seconds: int = 45
    meeting_live_whisper_model: str = "small"
    meeting_final_whisper_model: str = "large-v3-turbo"
    meeting_local_final_whisper_model: str = "small"
    meeting_intelligence_local_url: str = "http://127.0.0.1:8110"
    meeting_intelligence_remote_url: Optional[str] = None
    meeting_intelligence_api_key: Optional[SecretStr] = None
    meeting_intelligence_timeout_seconds: float = 300.0
    meeting_intelligence_autostart: bool = False
    meeting_intelligence_startup_timeout_seconds: float = 180.0
    voiceprint_match_threshold: float = 0.72
    voiceprint_match_margin: float = 0.05
    # Dangerous-task admin activation (voice + local numeric PIN).
    admin_voice_worker_url: Optional[str] = None
    admin_voice_enabled: bool = False
    headless_mode: bool = False
    hands_free_mode: bool = False

    # Narrow personal Amazon.in Cash-on-Delivery rehearsal.  This stays off
    # unless explicitly enabled, has a hard code-level ₹500 ceiling, and only
    # ever operates through the deterministic tracked-Chrome adapter.
    amazon_commerce_enabled: bool = False
    amazon_commerce_browser: Literal["Google Chrome"] = "Google Chrome"
    amazon_commerce_max_total_inr: int = 500
    amazon_commerce_session_ttl_seconds: int = 900
    # When enabled, Amazon item opening requires a fresh local screenshot/OCR
    # check as well as a tracked Chrome viewport check.  No image or OCR text
    # is persisted.
    amazon_commerce_require_screen_observation: bool = False

    # Cognitive memory
    working_memory_max_items: int = 50
    working_memory_ttl_minutes: int = 30
    
    # Memory Configuration
    chroma_persist_dir: Path = Field(
        default=Path.home() / ".saksham" / "memory",
        description="Directory for ChromaDB persistence"
    )
    
    # Database
    database_url: str = "sqlite+aiosqlite:///~/.saksham/saksham.db"
    
    # Operating Mode
    default_mode: Literal["assist", "execute", "autonomous", "shadow"] = "assist"
    
    # Mac Integration
    mac_automation_enabled: bool = False
    require_confirmation_for_destructive: bool = True

    # Apple Music catalog (MusicKit developer token; keep this server-side)
    apple_music_developer_token: Optional[SecretStr] = None
    apple_music_storefront: str = "in"
    apple_music_catalog_timeout_seconds: float = 10.0

    # YouTube music fallback
    youtube_music_fallback_enabled: bool = True
    youtube_api_key: Optional[SecretStr] = None
    youtube_search_timeout_seconds: float = 10.0
    youtube_browser: Literal["Google Chrome", "Safari"] = "Google Chrome"
    
    # Audit & Logging
    audit_log_path: Path = Field(
        default=Path.home() / ".saksham" / "logs" / "audit.log",
        description="Path to audit log file"
    )
    log_level: str = "INFO"
    
    @property
    def saksham_home(self) -> Path:
        """Base directory for all Saksham data"""
        return Path.home() / ".saksham"

    @property
    def database_path(self) -> Path:
        """Resolve the configured SQLite database path on disk."""
        for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
            if self.database_url.startswith(prefix):
                return Path(self.database_url[len(prefix):]).expanduser()
        return Path(self.database_url).expanduser()
    
    def ensure_directories(self) -> None:
        """Create necessary directories if they don't exist"""
        self.saksham_home.mkdir(parents=True, exist_ok=True)
        self.chroma_persist_dir.mkdir(parents=True, exist_ok=True)
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        (self.saksham_home / "meetings").mkdir(parents=True, exist_ok=True)
        self.projects_root.expanduser().mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    """
    Get cached settings instance.
    Uses LRU cache to avoid reloading configuration.
    """
    settings = Settings()
    settings.ensure_directories()
    return settings
