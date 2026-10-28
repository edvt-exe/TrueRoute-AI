import unicodedata
from collections.abc import Callable

from app.auth.passwords import normalize_password

MIN_LENGTH = 12
# bounds hashing work, long passphrases still fit
MAX_LENGTH = 128
# shorter usernames match too many passwords by chance
MIN_PERSONAL = 3


def _has_control(password: str) -> bool:
    # cc covers line breaks and nul, cf covers invisible format characters
    return any(unicodedata.category(ch) in ("Cc", "Cf") for ch in password)


# every type is required, each missing one gets its own message
CHARACTER_TYPES: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("a lowercase letter", str.islower),
    ("an uppercase letter", str.isupper),
    ("a digit", str.isdigit),
    ("a symbol", lambda ch: not ch.isalnum()),
)


def uses_personal_data(password: str, *, username: str, email: str) -> bool:
    folded = normalize_password(password).casefold()
    personal = (username, email.partition("@")[0])
    return any(len(p) >= MIN_PERSONAL and p.casefold() in folded for p in personal)


def password_problems(password: str, *, username: str = "", email: str = "") -> list[str]:
    # messages never include the password, they reach the client and may reach logs
    value = normalize_password(password)
    problems: list[str] = []
    if len(value) < MIN_LENGTH:
        problems.append(f"must be at least {MIN_LENGTH} characters")
    if len(value) > MAX_LENGTH:
        problems.append(f"must be at most {MAX_LENGTH} characters")
    if _has_control(value):
        problems.append("must not contain line breaks or invisible characters")
    for name, is_type in CHARACTER_TYPES:
        if not any(is_type(ch) for ch in value):
            problems.append(f"must include {name}")
    if uses_personal_data(password, username=username, email=email):
        problems.append("must not contain your username or email")
    return problems