"""Runtime settings, read from environment variables.

Secrets (database password, alert URLs) come from the environment or a local .env file
that git ignores. They are typed as SecretStr so an accidental print or log of the
settings object shows '**********' instead of the value.
"""

from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    # e.g. postgresql+psycopg://homewatch:<password>@db:5432/homewatch
    database_url: SecretStr
    log_level: str = "INFO"
    expose_docs: bool = True  # Swagger UI at /docs; turn off anywhere public

    # Watchdog
    watchdog_interval_s: float = 30.0
    alert_timezone: str = "America/New_York"
    # Optional phone push for local testing (https://ntfy.sh). The topic URL acts as a
    # password — anyone who knows it can read the alerts — so it lives in .env only.
    ntfy_url: SecretStr | None = None
    ntfy_token: SecretStr | None = None

    @field_validator("ntfy_url", "ntfy_token", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        # Docker Compose passes unset variables as empty strings.
        return None if value == "" else value
    
    @field_validator("ntfy_url")
    @classmethod
    def _ntfy_url_is_web_address(cls, value: SecretStr | None) -> SecretStr | None:
        # Fail at startup, not at the first alert. In the pilot, a URL without https://
        # made every delivery fail with ValueError while the watchdog looked healthy.
        if value is not None and not value.get_secret_value().startswith(("https://", "http://")):
            raise ValueError("must start with https://, like https://ntfy.sh/your-topic")
        return value

@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
