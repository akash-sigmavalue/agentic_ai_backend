from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or a local .env file."""

    openai_api_key: str | None = Field(None, validation_alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o-mini", validation_alias="OPENAI_MODEL")
    bedrock_api_key: str | None = Field(None, validation_alias="BEDROCK_API_KEY")
    bedrock_region: str = Field("ap-south-1", validation_alias="BEDROCK_REGION")
    bedrock_base_url: str | None = Field(None, validation_alias="BEDROCK_BASE_URL")
    transaction_database_url: str | None = Field(
        None, validation_alias="DATABASE_URL"
    )
    sql_probe_sample_limit: int = Field(5, ge=1, validation_alias="SQL_PROBE_SAMPLE_LIMIT")
    sql_probe_output_limit: int = Field(5, ge=1, validation_alias="SQL_PROBE_OUTPUT_LIMIT")
    react_max_iterations: int = Field(3, ge=1, validation_alias="REACT_MAX_ITERATIONS")
    app_name: str = "Real Estate SQL Agent API"
    log_level: str = Field("INFO", validation_alias="LOG_LEVEL")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
