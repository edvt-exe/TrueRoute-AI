import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, LargeBinary, MetaData, String, false, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# fixed constraint names, the migration and the duplicate checks refer to them
NAMING = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}
DIGEST_BYTES = 32
OTP_PURPOSES = ("verify_email", "signin_2fa", "reset_password")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _timestamp() -> DateTime:
    # always timestamptz, naive times are a classic expiry bug
    return DateTime(timezone=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # as typed, shown to the user
    username: Mapped[str] = mapped_column(String(32))
    # casefolded copy, the unique index makes Ana and ana the same name
    username_key: Mapped[str] = mapped_column(String(32), unique=True)
    # lowercased by the request schema
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    email_verified_at: Mapped[datetime | None] = mapped_column(_timestamp())
    two_factor_enabled: Mapped[bool] = mapped_column(default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(_timestamp(), server_default=func.now())


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (CheckConstraint(f"octet_length(token_hash) = {DIGEST_BYTES}", name="token_hash_length"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # sha-256 of the cookie token, a leaked table does not hand out live sessions
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(DIGEST_BYTES), unique=True)
    created_at: Mapped[datetime] = mapped_column(_timestamp(), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(_timestamp(), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(_timestamp(), index=True)


class OtpChallenge(Base):
    __tablename__ = "otp_challenges"
    __table_args__ = (
        CheckConstraint(f"purpose IN ({', '.join(repr(p) for p in OTP_PURPOSES)})", name="purpose"),
        CheckConstraint("attempts >= 0", name="attempts"),
        CheckConstraint(f"octet_length(code_hmac) = {DIGEST_BYTES}", name="code_hmac_length"),
    )

    # random url-safe id handed to the browser, useless without the emailed code
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(16))
    # hmac with a server key, a million possible codes are too few for a plain hash
    code_hmac: Mapped[bytes] = mapped_column(LargeBinary(DIGEST_BYTES))
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(_timestamp())
    consumed_at: Mapped[datetime | None] = mapped_column(_timestamp())
    created_at: Mapped[datetime] = mapped_column(_timestamp(), server_default=func.now())