from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User

UQ_USERNAME = "uq_users_username_key"
UQ_EMAIL = "uq_users_email"


class UsernameTakenError(Exception):
    pass


class EmailTakenError(Exception):
    pass


def _constraint(exc: IntegrityError) -> str | None:
    # asyncpg keeps the violated constraint on the driver error under the sqlalchemy wrapper
    for err in (exc.orig, getattr(exc.orig, "__cause__", None)):
        name = getattr(err, "constraint_name", None)
        if isinstance(name, str):
            return name
    return None


async def create_user(session: AsyncSession, *, username: str, email: str, password_hash: str) -> User:
    user = User(username=username, username_key=username.casefold(), email=email.lower(), password_hash=password_hash)
    try:
        # a savepoint, so a duplicate leaves the caller's transaction usable
        async with session.begin_nested():
            session.add(user)
    except IntegrityError as exc:
        # from None, the driver message quotes the duplicate value
        name = _constraint(exc)
        if name == UQ_USERNAME:
            raise UsernameTakenError() from None
        if name == UQ_EMAIL:
            raise EmailTakenError() from None
        raise
    return user


async def find_user(session: AsyncSession, identifier: str) -> User | None:
    # usernames cannot contain @, so the two lookups never overlap
    value = identifier.strip()
    if "@" in value:
        query = select(User).where(User.email == value.lower())
    else:
        query = select(User).where(User.username_key == value.casefold())
    return (await session.execute(query)).scalar_one_or_none()