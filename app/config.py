"""Application configuration using pydantic-settings.

All environment variables are loaded from .env file or environment.
No hardcoded secrets, URLs, or credentials.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Redis configuration (CONTRACT §3)
    redis_host: str = Field(default="localhost", description="Redis host")
    redis_port: int = Field(default=6379, description="Redis port")
    redis_password: str = Field(default="", description="Redis password (requirepass)")
    redis_db: int = Field(default=0, description="Redis database number")

    # PostgreSQL configuration (CONTRACT §3)
    postgres_host: str = Field(default="localhost", description="PostgreSQL host")
    postgres_port: int = Field(default=5432, description="PostgreSQL port")
    postgres_user: str = Field(default="dancebot", description="PostgreSQL user")
    postgres_password: str = Field(..., description="PostgreSQL password (required)")
    postgres_db: str = Field(default="dancebot", description="PostgreSQL database name")

    # CRM Impulse configuration (CONTRACT §5)
    crm_tenant: str = Field(..., description="Impulse CRM tenant (e.g., 'studio')")
    crm_api_key: str = Field(..., description="Impulse CRM API key for HTTP Basic auth")

    # Telegram configuration (CONTRACT §19)
    telegram_bot_token: str = Field(..., description="Telegram bot token")
    telegram_secret_token: str = Field(..., description="Telegram webhook secret_token for signature verification")
    admin_telegram_chat_id: str = Field(..., description="Admin Telegram chat ID for alerts and handoff")

    # WhatsApp configuration (CONTRACT §19)
    whatsapp_api_key: str = Field(default="", description="WhatsApp Cloud API key")
    whatsapp_webhook_secret: str = Field(default="", description="WhatsApp webhook secret (X-Hub-Signature-256)")

    # LLM configuration (CONTRACT §12)
    yandexgpt_api_key: str = Field(..., description="YandexGPT Pro 5.1 API key (primary LLM)")
    yandexgpt_folder_id: str = Field(..., description="YandexGPT folder ID (b1g...)")
    anthropic_api_key: str = Field(default="", description="Anthropic Claude API key (optional fallback)")

    # Budget Guard limits (CONTRACT §12)
    max_tokens_per_hour: int = Field(default=100_000, description="Maximum tokens per hour")
    max_cost_per_day_usd: float = Field(default=10.0, description="Maximum cost per day in USD")
    max_requests_per_minute: int = Field(default=30, description="Maximum LLM requests per minute")
    max_errors_per_hour: int = Field(default=50, description="Maximum errors per hour")

    # Knowledge Base configuration (CONTRACT §15)
    kb_file_path: str = Field(
        default="knowledge/studio.yaml",
        description="Path to knowledge base YAML file",
    )

    # Timezone configuration (CONTRACT §7)
    timezone: str = Field(
        default="Asia/Vladivostok",
        description="Timezone for datetime resolution",
    )

    # Test mode (CONTRACT §17)
    test_mode: bool = Field(
        default=False,
        description="Test mode: mock CRM, full trace stdout, debug commands",
    )

    # TTL configuration (CONTRACT §4)
    session_ttl_hours: int = Field(default=24, description="Session TTL in hours")
    user_profile_ttl_days: int = Field(default=90, description="User profile TTL in days")

    # Application settings
    app_name: str = Field(default="DanceBot", description="Application name")
    log_level: str = Field(default="INFO", description="Logging level")

    @property
    def redis_url(self) -> str:
        """Construct Redis URL."""
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def postgres_url(self) -> str:
        """Construct PostgreSQL DSN (plain format for asyncpg)."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def crm_base_url(self) -> str:
        """Construct Impulse CRM base URL (CONTRACT §5)."""
        return f"https://{self.crm_tenant}.impulsecrm.ru/api"


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance.

    Using @lru_cache prevents crash on import when .env is missing (e.g. during tests).
    Settings are loaded lazily on first access.
    """
    return Settings()

