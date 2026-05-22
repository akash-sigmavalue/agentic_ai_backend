from __future__ import annotations

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "AI Agent Dynamic Backend"

    # DB
    DATABASE_URL: str
    DATABASE_URL_RDS: str
    # JWT Auth
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # LLM Settings
    OPENAI_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None

    # CORS
    BACKEND_CORS_ORIGINS: list[str] = ["*"]

    # Google OAuth Settings
    GOOGLE_OAUTH_CLIENT_ID: Optional[str] = None
    GOOGLE_OAUTH_CLIENT_SECRET: Optional[str] = None
    GOOGLE_OAUTH_REDIRECT_URI: Optional[str] = None
    GOOGLE_MAPS_API_KEY: Optional[str] = None

    # Gmail Pub/Sub / Push Notifications
    GMAIL_PUBSUB_PROJECT_ID: Optional[str] = None
    GMAIL_PUBSUB_TOPIC_NAME: Optional[str] = None
    GMAIL_PUBSUB_WEBHOOK_TOKEN: Optional[str] = None
    GMAIL_PUBSUB_ENABLE: bool = True

    THREE_D_MAPS_EXCEL_PATH: Optional[str] = None
    THREE_D_MAPS_TIMELAPSE_EXCEL_PATH: Optional[str] = None
    SPATIAL_ANALYSIS_EXCEL_PATH: Optional[str] = None
    COLUMN_MAPPING_PATH: Optional[str] = None
    
    #portfolio relate
    OPENAI_MODEL: str = "gpt-4o-mini"
    MAX_PREVIEW_ROWS: int = 6
    DEBUG_MAPPING_AGENT: bool = True
    MAPPING_AGENT_REVIEW_ENABLED: bool = True
    MAPPING_AGENT_REPAIR_ROUNDS: int = 2
    UPLOAD_DIR: str = "storage/uploads"
    TEMP_DIR: str = "storage/temp"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )


settings = Settings()
