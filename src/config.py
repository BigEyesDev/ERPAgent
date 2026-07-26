"""Application configuration, loaded once from the environment.

The only place in the codebase allowed to read `os.environ` / `.env`
directly. Every other module receives configuration through the `settings`
singleton exported here, never through its own `os.getenv` call.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Typed, validated application settings sourced from `.env` / environment."""

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    openrouter_api_key: str = Field(alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    openrouter_model: str = Field(
        default="openai/gpt-4o-mini", alias="OPENROUTER_MODEL"
    )
    llm_timeout_seconds: float = Field(default=30.0, alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=1, alias="LLM_MAX_RETRIES")

    erp_base_url: str = Field(default="mock://erp", alias="ERP_BASE_URL")
    audit_path: str = Field(default=str(_REPO_ROOT / "data" / "audit.jsonl"), alias="AUDIT_PATH")

    mlflow_tracking_uri: str = Field(
        default=f"sqlite:///{_REPO_ROOT / 'data' / 'mlflow.db'}", alias="MLFLOW_TRACKING_URI"
    )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide `Settings` instance, constructed once."""
    return Settings()


settings = get_settings()
