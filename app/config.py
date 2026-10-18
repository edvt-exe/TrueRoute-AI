import logging
import re
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.auth.captcha import check_turnstile_key
from app.db.engine import check_database_url
from app.services.geocoder import DEFAULT_BASE_URL as NOMINATIM_DEFAULT_URL
from app.services.llm_agent import ALLOWED_EFFORTS, DEFAULT_EFFORT, DEFAULT_MODEL, is_model_id
from app.services.overpass_client import DEFAULT_BASE_URL as OVERPASS_DEFAULT_URL
from app.security import DEFAULT_DAILY_CAP, DEFAULT_PER_IP_HOUR, check_access_code
from app.services.osrm_routes import DEFAULT_BASE_URL as OSRM_DEFAULT_URL
from app.services.walking_routes import DEFAULT_BASE_URL as ORS_DEFAULT_URL
from app.utils.http_guard import check_base_url, check_user_agent

# anchored to the repo root so it works from any working directory
DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
# these log full urls or request bodies at debug, keep them quiet whatever the app level
QUIET_LOGGERS = ("anthropic", "httpx2", "httpx", "httpcore")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MIN_OTP_KEY = 32
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
    # optional, without it every day uses aerial distances
    ors_api_key: SecretStr | None = None
    ors_base_url: str = ORS_DEFAULT_URL
    # backup walking routes when ors is down, slow or has no key, fossgis public osrm by default
    osrm_enabled: bool = True
    osrm_base_url: str = OSRM_DEFAULT_URL
    # optional, when set the itinerary endpoint needs the X-Access-Code header
    demo_access_code: SecretStr | None = None
    # assumption: demo sized limits, the daily cap keeps llm and ors spend bounded
    rate_limit_per_ip_hour: int = Field(default=DEFAULT_PER_IP_HOUR, ge=1, le=1000)
    daily_itinerary_cap: int = Field(default=DEFAULT_DAILY_CAP, ge=1, le=10_000)
    log_level: LogLevel = "INFO"
    # optional until accounts ship, it holds the database password
    database_url: SecretStr | None = None
    # cloudflare turnstile, both or neither, the site key is public, the secret is not
    turnstile_site_key: str | None = None
    turnstile_secret_key: SecretStr | None = None
    # hmac key for one-time codes, at least 32 random characters, changing it voids open codes
    otp_hmac_key: SecretStr | None = None

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

    @field_validator("ors_api_key", "demo_access_code", "database_url", "turnstile_site_key", "turnstile_secret_key", "otp_hmac_key", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        # the empty line from .env.example means the feature is off
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("ors_api_key")
    @classmethod
    def _check_ors_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if not raw.isprintable() or any(ch.isspace() for ch in raw):
            raise ValueError("must not contain spaces or line breaks")
        return value

    @field_validator("demo_access_code")
    @classmethod
    def _check_access_code(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            check_access_code(value.get_secret_value())
        return value

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            check_database_url(value.get_secret_value())
        return value

    @field_validator("turnstile_site_key")
    @classmethod
    def _check_site_key(cls, value: str | None) -> str | None:
        return None if value is None else check_turnstile_key(value)

    @field_validator("turnstile_secret_key")
    @classmethod
    def _check_secret_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            check_turnstile_key(value.get_secret_value())
        return value

    @field_validator("otp_hmac_key")
    @classmethod
    def _check_otp_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if len(raw) < MIN_OTP_KEY or not raw.isprintable() or any(ch.isspace() for ch in raw):
            raise ValueError(f"must be at least {MIN_OTP_KEY} printable characters without spaces")
        return value

    @model_validator(mode="after")
    def _accounts_all_or_nothing(self) -> Self:
        # accounts need the database, the human check and the code key together, never part of them
        needed = {"DATABASE_URL": self.database_url, "TURNSTILE_SECRET_KEY": self.turnstile_secret_key, "OTP_HMAC_KEY": self.otp_hmac_key}
        missing = [name for name, value in needed.items() if value is None]
        if missing and len(missing) < len(needed):
            raise ValueError(f"accounts also need {', '.join(missing)}")
        return self

    @property
    def accounts_enabled(self) -> bool:
        return self.database_url is not None

    @model_validator(mode="after")
    def _turnstile_pair(self) -> Self:
        if (self.turnstile_site_key is None) != (self.turnstile_secret_key is None):
            raise ValueError("TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY must be set together")
        return self

    @field_validator("nominatim_base_url", "overpass_base_url", "ors_base_url", "osrm_base_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return check_base_url(_no_key(value))

    @model_validator(mode="after")
    def _ors_key_not_in_url(self) -> Self:
        # the key belongs in the header only, a url ends up in logs and proxies
        if self.ors_api_key is not None and self.ors_api_key.get_secret_value() in self.ors_base_url:
            raise ValueError("ORS_BASE_URL must not contain the ORS key")
        return self

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