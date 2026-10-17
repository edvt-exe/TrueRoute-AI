# users, sign-in sessions and one-time codes
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, *, nullable: bool = False, default: bool = False) -> sa.Column:
    server_default = sa.func.now() if default else None
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, server_default=server_default)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("username_key", sa.String(32), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        _ts("email_verified_at", nullable=True),
        sa.Column("two_factor_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        _ts("created_at", default=True),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username_key", name="uq_users_username_key"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(32), nullable=False),
        _ts("created_at", default=True),
        _ts("last_seen_at", default=True),
        _ts("expires_at"),
        sa.PrimaryKeyConstraint("id", name="pk_auth_sessions"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_auth_sessions_user_id", ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        sa.CheckConstraint("octet_length(token_hash) = 32", name="ck_auth_sessions_token_hash_length"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_table(
        "otp_challenges",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("code_hmac", sa.LargeBinary(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _ts("expires_at"),
        _ts("consumed_at", nullable=True),
        _ts("created_at", default=True),
        sa.PrimaryKeyConstraint("id", name="pk_otp_challenges"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_otp_challenges_user_id", ondelete="CASCADE"),
        sa.CheckConstraint("purpose IN ('verify_email', 'signin_2fa', 'reset_password')", name="ck_otp_challenges_purpose"),
        sa.CheckConstraint("attempts >= 0", name="ck_otp_challenges_attempts"),
        sa.CheckConstraint("octet_length(code_hmac) = 32", name="ck_otp_challenges_code_hmac_length"),
    )
    op.create_index("ix_otp_challenges_user_id", "otp_challenges", ["user_id"])


def downgrade() -> None:
    op.drop_table("otp_challenges")
    op.drop_table("auth_sessions")
    op.drop_table("users")