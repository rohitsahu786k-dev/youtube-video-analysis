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
    GEMINI_CHAT_MODEL: str = "gemini-3.1-flash-lite"      # chat query-rewrite + answers: ~2x faster and ~3x cheaper than the analysis model, answers stay evidence-validated

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
    MAX_CHANNEL_VIDEOS: int = Field(default=3000, ge=1, le=20000)
    INCLUDE_SHORTS: bool = False
    SCENE_DETECTION: bool = True
    MAX_VIDEO_ATTEMPTS: int = Field(default=3, ge=1, le=10)
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
    GEMINI_PRICING_JSON: Optional[str] = None            # {"model": {"input": usd_per_1M, "output": usd_per_1M}} overrides the built-in price table
    AI_DAILY_BUDGET_USD: float = Field(default=3.0, ge=0)    # per user per day; new analysis pauses and chat turns extractive at the cap (0 = unlimited)
    AI_DAILY_QUESTIONS: int = Field(default=80, ge=0)        # chat questions per user per day that may use the language model (0 = unlimited)
    AI_MAX_COST_PER_VIDEO_USD: float = Field(default=1.0, ge=0)   # hard ceiling on one video's own Gemini spend, independent of the daily cap (0 = unlimited)
    LOG_LEVEL: str = "info"

    # Abuse and storage protection
    RATE_LIMIT_ENABLED: bool = True
    MIN_FREE_DISK_MB: int = Field(default=3072, ge=0)        # refuse uploads / new downloads below this much free space
    MIN_FREE_INODE_PCT: float = Field(default=8, ge=0, le=50)
    MEDIA_QUOTA_MB: int = Field(default=3000, ge=50)         # per-user media library quota
    MAX_QUEUED_JOBS: int = Field(default=25, ge=1)           # per-user background jobs waiting or running
    JOB_RETENTION_DAYS: int = Field(default=30, ge=1)

    # Security
    SECRET_KEY: Optional[str] = None
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: Optional[str] = None
    ALLOW_OPEN_ACCESS: bool = False
    SESSION_HOURS: int = Field(default=168, ge=1, le=24 * 90)
    LOGIN_MAX_FAILURES: int = Field(default=6, ge=3, le=50)
    LOGIN_LOCK_MINUTES: int = Field(default=15, ge=1, le=1440)

    # Market data / integrations
    BINANCE_BASE_URL: str = "https://api.binance.com"
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None
    DISPLAY_TZ: str = "Asia/Kolkata"
    MAX_UPLOAD_MB: int = Field(default=200, ge=1, le=4096)
    BACKUP_KEEP: int = Field(default=14, ge=1, le=365)
    BACKTEST_PROCESSES: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False
    )

    @property
    def effective_api_key(self) -> Optional[str]:
        return self.GEMINI_API_KEY

    @property
    def auth_required(self) -> bool:
        return bool(self.ADMIN_PASSWORD) or (self.APP_ENV.lower() == "production" and not self.ALLOW_OPEN_ACCESS)

    @property
    def secure_cookies(self) -> bool:
        return self.APP_URL.lower().startswith("https://")

settings = Settings()
