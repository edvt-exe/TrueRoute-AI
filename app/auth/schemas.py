import hmac
import re
from typing import Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator, model_validator

from app.auth.password_policy import password_problems

USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{3,32}")
# names that could pass for staff or break urls
RESERVED_USERNAMES = frozenset(
    {"admin", "administrator", "root", "support", "help", "system", "api", "auth", "null", "undefined", "trueroute"}
)
MAX_EMAIL = 254
MAX_IDENTIFIER = 254
# nfkc can shrink text, so the raw cap sits above the policy maximum
MAX_PASSWORD_RAW = 256
# cloudflare turnstile tokens are at most 2048 characters
MAX_CAPTCHA_TOKEN = 2048
OTP_PATTERN = r"^[0-9]{6}$"
CHALLENGE_PATTERN = r"^[A-Za-z0-9_-]{22,64}$"


class _Strict(BaseModel):
    # strict types, unknown fields refused, submitted values never echoed in errors
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)


def _raw_password(value: SecretStr) -> SecretStr:
    if len(value.get_secret_value()) > MAX_PASSWORD_RAW:
        raise ValueError(f"must be at most {MAX_PASSWORD_RAW} characters")
    return value


def _capped_email(value: object) -> object:
    # cheap length check before the full address parser runs
    if isinstance(value, str) and len(value) > MAX_EMAIL:
        raise ValueError(f"must be at most {MAX_EMAIL} characters")
    return value


def _same_matching(first: SecretStr, second: SecretStr) -> bool:
    # constant time, the comparison must not leak how much of the confirmation matched
    return hmac.compare_digest(first.get_secret_value().encode(), second.get_secret_value().encode())


class SignUpRequest(_Strict):
    username: str
    email: EmailStr
    password: SecretStr
    password_confirm: SecretStr
    captcha_token: str = Field(min_length=1, max_length=MAX_CAPTCHA_TOKEN)

    @field_validator("username")
    @classmethod
    def _check_username(cls, value: str) -> str:
        # fullmatch, a plain $ would let a trailing line break through
        if not USERNAME_PATTERN.fullmatch(value):
            raise ValueError("must be 3 to 32 letters, digits or underscores")
        if value.casefold() in RESERVED_USERNAMES:
            raise ValueError("is reserved, choose another")
        return value

    @field_validator("email", mode="before")
    @classmethod
    def _cap_email(cls, value: object) -> object:
        return _capped_email(value)

    @field_validator("email")
    @classmethod
    def _lower_email(cls, value: str) -> str:
        # one account per address whatever the letter case
        return value.lower()

    @field_validator("password", "password_confirm")
    @classmethod
    def _cap_password(cls, value: SecretStr) -> SecretStr:
        return _raw_password(value)

    @model_validator(mode="after")
    def _check_password(self) -> Self:
        problems = password_problems(self.password.get_secret_value(), username=self.username, email=self.email)
        if problems:
            raise ValueError(f"password {'; '.join(problems)}")
        if not _same_matching(self.password, self.password_confirm):
            raise ValueError("passwords do not match")
        return self

    @property
    def username_key(self) -> str:
        # unique index column, so Ana and ana cannot both register
        return self.username.casefold()


class SignInRequest(_Strict):
    # username or email, the policy is not applied so older passwords keep working
    identifier: str = Field(min_length=1, max_length=MAX_IDENTIFIER)
    password: SecretStr
    # required by the server only after repeated failures
    captcha_token: str | None = Field(default=None, min_length=1, max_length=MAX_CAPTCHA_TOKEN)

    @field_validator("password")
    @classmethod
    def _cap_password(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("must not be empty")
        return _raw_password(value)


class OtpVerifyRequest(_Strict):
    challenge_id: str = Field(pattern=CHALLENGE_PATTERN)
    code: str = Field(pattern=OTP_PATTERN)


class ResendRequest(_Strict):
    challenge_id: str = Field(pattern=CHALLENGE_PATTERN)


class ForgotPasswordRequest(_Strict):
    email: EmailStr
    captcha_token: str = Field(min_length=1, max_length=MAX_CAPTCHA_TOKEN)

    @field_validator("email", mode="before")
    @classmethod
    def _cap_email(cls, value: object) -> object:
        return _capped_email(value)

    @field_validator("email")
    @classmethod
    def _lower_email(cls, value: str) -> str:
        return value.lower()


class ResetPasswordRequest(_Strict):
    challenge_id: str = Field(pattern=CHALLENGE_PATTERN)
    code: str = Field(pattern=OTP_PATTERN)
    password: SecretStr
    password_confirm: SecretStr

    @field_validator("password", "password_confirm")
    @classmethod
    def _cap_password(cls, value: SecretStr) -> SecretStr:
        return _raw_password(value)

    @model_validator(mode="after")
    def _check_password(self) -> Self:
        # the username and email rule runs in the route after the code is checked, this schema cannot
        # know the account, and answering it earlier would tell a real challenge from a decoy
        problems = password_problems(self.password.get_secret_value())
        if problems:
            raise ValueError(f"password {'; '.join(problems)}")
        if not _same_matching(self.password, self.password_confirm):
            raise ValueError("passwords do not match")
        return self


class TwoFactorRequest(_Strict):
    enabled: bool
    # the current password again, so a stolen session alone cannot switch 2fa off
    password: SecretStr

    @field_validator("password")
    @classmethod
    def _cap_password(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("must not be empty")
        return _raw_password(value)