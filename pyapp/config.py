import os
from typing import Optional, Literal
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    APP_ENV: str = "development"
    APP_URL: str = "http://localhost:8000"
    NEXT_PUBLIC_APP_URL: Optional[str] = "http://localhost:8000"
    MOCK_AI_MODE: bool = False

    # Gemini Configuration
    GEMINI_API_KEY: Optional[str] = Field(default=None, alias="GEMINI_API_KEY")
    OPENAI_API_KEY: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")
    GEMINI_ANALYSIS_MODEL: str = "gemini-3.8-flash"
    GEMINI_REASONING_MODEL: str = "gemini-3.8-flash"
    GEMINI_FINAL_MODEL: str = "gemini-3.8-flash"
    GEMINI_TRANSCRIBE_MODEL: str = "gemini-3.8-flash"
    GEMINI_EMBEDDING_MODEL: str = "gemini-embedding-001"

    # External Services
    YOUTUBE_API_KEY: Optional[str] = None
    DATABASE_URL: Optional[str] = None
    CLOUDINARY_URL: Optional[str] = None
    STORAGE_PATH: str = "./storage"

    # TradingView Integration
    DEFAULT_CHART_SYMBOL: str = "FX:EURUSD"
    OANDA_API_TOKEN: Optional[str] = None
    OANDA_ACCOUNT_ID: Optional[str] = None
    OANDA_ENVIRONMENT: Literal["practice", "live"] = "practice"
    YTDLP_COOKIES_PATH: Optional[str] = None
    FFMPEG_PATH: Optional[str] = None
    MAX_PLAYLIST_VIDEOS: int = Field(default=500, ge=1, le=2000)
    AUDIO_CHUNK_SECONDS: int = Field(default=600, ge=60, le=1200)
    MIN_HOLDOUT_TRADES: int = Field(default=30, ge=10)
    ENABLE_WORKER: bool = True
    MIN_EXTRACTION_CONFIDENCE: float = Field(default=0.8, ge=0, le=1)

    # Processing Concurrency & Limits
    AI_PROCESSING_MODE: str = "standard"
    VIDEO_PROCESS_CONCURRENCY: int = Field(default=2, ge=1, le=4)
    AI_ANALYSIS_CONCURRENCY: int = 5
    FRAME_ANALYSIS_CONCURRENCY: int = 3
    FRAME_EVENT_BEFORE_SECONDS: int = Field(default=15, ge=0, le=300)
    FRAME_EVENT_AFTER_SECONDS: int = Field(default=30, ge=0, le=300)
    FRAME_INTERVAL_SECONDS: int = Field(default=5, ge=1, le=60)
    MAX_FRAMES_PER_EVENT: int = Field(default=8, ge=3, le=16)
    MAX_CANDIDATE_EVENTS_PER_VIDEO: int = Field(default=100, ge=1, le=2000)

    # Cost & Logging
    GEMINI_COST_TRACKING: bool = True
    LOG_LEVEL: str = "info"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False
    )

    @property
    def effective_api_key(self) -> Optional[str]:
        return self.GEMINI_API_KEY

settings = Settings()
