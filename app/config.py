import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.geocoder import DEFAULT_BASE_URL as NOMINATIM_DEFAULT_URL
from app.services.llm_agent import ALLOWED_EFFORTS, DEFAULT_EFFORT, DEFAULT_MODEL, is_model_id
from app.services.overpass_client import DEFAULT_BASE_URL as OVERPASS_DEFAULT_URL
from app.utils.http_guard import check_base_url, check_user_agent

# anchored to the repo root so it works from any working directory
DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
# these log full urls or request bodies at debug, keep them quiet whatever the app level
QUIET_LOGGERS = ("anthropic", "httpx2", "httpx", "httpcore")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# anthropic keys start with this, a key in a public setting would go to third parties
KEY_PREFIX = "sk-ant-"


def _no_key(value: str) -> str:
    if KEY_PREFIX in value.lower():
        raise ValueError("must not contain an API key")
    return value


class ConfigError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
        # defence in depth, pydantic never prints input values in its errors
        hide_input_in_errors=True,
    )

    anthropic_api_key: SecretStr
    llm_model: str = DEFAULT_MODEL
    llm_effort: str = DEFAULT_EFFORT
    app_user_agent: str
    nominatim_base_url: str = NOMINATIM_DEFAULT_URL
    overpass_base_url: str = OVERPASS_DEFAULT_URL
    log_level: LogLevel = "INFO"

    @field_validator("anthropic_api_key")
    @classmethod
    def _check_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw or not raw.isprintable() or any(ch.isspace() for ch in raw):
            raise ValueError("must be set, without spaces or line breaks")
        return value

    @field_validator("llm_model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        if not is_model_id(value):
            raise ValueError("must be a plain model id such as claude-sonnet-5-5")
        return value

    @field_validator("llm_effort")
    @classmethod
    def _check_effort(cls, value: str) -> str:
        if value not in ALLOWED_EFFORTS:
            raise ValueError(f"must be one of {', '.join(ALLOWED_EFFORTS)}")
        return value

    @field_validator("app_user_agent")
    @classmethod
    def _check_agent(cls, value: str) -> str:
        # sent to nominatim and overpass with every request
        return check_user_agent(_no_key(value))

    @field_validator("nominatim_base_url", "overpass_base_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return check_base_url(_no_key(value))

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


def _describe(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors(include_input=False, include_url=False, include_context=False):
        name = str(err["loc"][0]) if err["loc"] else "settings"
        # a pasted secret on its own line becomes a key name, never echo odd names
        label = name.upper() if _ENV_NAME.match(name) else "an unrecognized line"
        parts.append(f"{label}: {err['msg']}")
    return "; ".join(parts)


def load_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> Settings:
    # real environment variables win over the file, a missing file is fine
    try:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as exc:
        raise ConfigError(f"invalid settings: {_describe(exc)}") from None


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(level=settings.log_level, format=LOG_FORMAT)
    logging.getLogger().setLevel(settings.log_level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)