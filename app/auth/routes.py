import logging
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.breach import BreachStatus
from app.auth.captcha import FailedSignIns
from app.auth.email import EmailMessage, EmailSender, code_email, existing_account_email, password_changed_email
from app.auth.errors import AuthError
from app.auth.otp import CHALLENGE_BYTES, OtpThrottledError, issue, retire_open, verify
from app.auth.password_policy import uses_personal_data
from app.auth.passwords import Passwords
from app.auth.schemas import (
    ForgotPasswordRequest,
    OtpVerifyRequest,
    ResendRequest,
    ResetPasswordRequest,
    SignInRequest,
    SignUpRequest,
    TwoFactorRequest,
)
from app.auth.sessions import (
    COOKIE_NAME,
    clear_session_cookie,
    create_session,
    current_user,
    revoke_all,
    revoke_session,
    set_session_cookie,
    utc_now,
)
from app.db.models import OtpChallenge, User
from app.db.users import EmailTakenError, UsernameTakenError, create_user, find_user
from app.security import client_key

logger = logging.getLogger(__name__)

# codes typed into the sign-in page, a reset code has its own routes and must never finish a sign-in
CODE_PURPOSES = ("verify_email", "signin_2fa")


class Captcha(Protocol):
    async def verify(self, token: str | None, *, action: str, remote_ip: str | None) -> bool: ...


class Breach(Protocol):
    async def check(self, password: str) -> BreachStatus: ...


@dataclass
class AuthServices:
    db: async_sessionmaker[AsyncSession]
    passwords: Passwords
    breach: Breach
    captcha: Captcha
    email: EmailSender
    otp_key: bytes
    failed: FailedSignIns = field(default_factory=FailedSignIns)
    clock: Callable[[], datetime] = utc_now


class CodeSent(BaseModel):
    status: Literal["otp_required"] = "otp_required"
    challenge_id: str


class SignedIn(BaseModel):
    status: Literal["signed_in"] = "signed_in"
    username: str


class PasswordChanged(BaseModel):
    status: Literal["password_changed"] = "password_changed"


class Account(BaseModel):
    username: str
    email: str
    two_factor_enabled: bool


def auth_services(request: Request) -> AuthServices:
    auth: AuthServices | None = getattr(request.app.state, "auth", None)
    if auth is None:
        raise AuthError("accounts_unavailable")
    return auth


def _ip(request: Request) -> str | None:
    # uvicorn already resolved trusted proxies into request.client
    return request.client.host if request.client else None


def _decoy() -> CodeSent:
    # looks exactly like a real challenge id, but no code exists for it
    return CodeSent(challenge_id=secrets.token_urlsafe(CHALLENGE_BYTES))


def _account(user: User) -> Account:
    return Account(username=user.username, email=user.email, two_factor_enabled=user.two_factor_enabled)


async def _send_after_response(sender: EmailSender, message: EmailMessage) -> None:
    # runs once the answer is out, so the mail provider's speed cannot show whether an account exists
    try:
        await sender.send(message)
    except Exception as exc:
        # the type only, a provider error can carry the message text and with it a live code
        logger.error("an account email could not be sent: %s", type(exc).__name__)


router = APIRouter(prefix="/auth")


@router.post("/signup", status_code=202, response_model=CodeSent)
async def signup(body: SignUpRequest, request: Request, auth: AuthServices = Depends(auth_services)) -> CodeSent:
    if not await auth.captcha.verify(body.captcha_token, action="signup", remote_ip=_ip(request)):
        raise AuthError("captcha_failed")
    password = body.password.get_secret_value()
    breach = await auth.breach.check(password)
    if breach is BreachStatus.BREACHED:
        raise AuthError("password_breached")
    if breach is BreachStatus.UNKNOWN:
        logger.warning("breached password check unavailable, sign-up allowed without it")
    # hashed before the lookup, so a taken email costs the same time as a new one
    hashed = await auth.passwords.hash_async(password)
    async with auth.db() as session:
        try:
            async with session.begin():
                user = await create_user(session, username=body.username, email=body.email, password_hash=hashed)
                challenge_id, code = await issue(session, user_id=user.id, purpose="verify_email", key=auth.otp_key, now=auth.clock())
        except UsernameTakenError:
            raise AuthError("username_taken") from None
        except EmailTakenError:
            # same answer as a new account, the real owner is told by email instead
            await auth.email.send(existing_account_email(body.email))
            return _decoy()
    await auth.email.send(code_email(user.email, "verify_email", code))
    logger.info("account created, waiting for email confirmation")
    return CodeSent(challenge_id=challenge_id)


@router.post("/otp/verify", response_model=SignedIn)
async def verify_code(
    body: OtpVerifyRequest, request: Request, response: Response, auth: AuthServices = Depends(auth_services)
) -> SignedIn:
    now = auth.clock()
    async with auth.db() as session:
        async with session.begin():
            query = select(OtpChallenge.purpose).where(OtpChallenge.id == body.challenge_id)
            purpose = (await session.execute(query)).scalar_one_or_none()
            user_id: uuid.UUID | None = None
            if purpose in CODE_PURPOSES:
                user_id = await verify(session, challenge_id=body.challenge_id, code=body.code, purpose=purpose, key=auth.otp_key, now=now)
        # the attempt is committed above, so a wrong guess counts even though it is refused
        if user_id is None:
            raise AuthError("invalid_code")
        async with session.begin():
            user = await session.get_one(User, user_id)
            if purpose == "verify_email" and user.email_verified_at is None:
                user.email_verified_at = now
            # a fresh session every sign-in, whatever cookie came in is retired
            await revoke_session(session, request.cookies.get(COOKIE_NAME))
            token = await create_session(session, user_id=user.id, now=now)
    set_session_cookie(response, token)
    return SignedIn(username=user.username)


@router.post("/signin", response_model=SignedIn | CodeSent)
async def signin(
    body: SignInRequest, request: Request, response: Response, auth: AuthServices = Depends(auth_services)
) -> SignedIn | CodeSent:
    ip = _ip(request)
    client = client_key(ip)
    if auth.failed.needs_captcha(account=body.identifier, client=client):
        if body.captcha_token is None:
            raise AuthError("captcha_required")
        if not await auth.captcha.verify(body.captcha_token, action="signin", remote_ip=ip):
            raise AuthError("captcha_failed")
    password = body.password.get_secret_value()
    now = auth.clock()
    async with auth.db() as session:
        async with session.begin():
            user = await find_user(session, body.identifier)
        # a missing account still costs one full hash, timing does not reveal which accounts exist
        if not await auth.passwords.verify_async(user.password_hash if user else None, password) or user is None:
            auth.failed.record_failure(account=body.identifier, client=client)
            raise AuthError("invalid_credentials")
        auth.failed.record_success(account=body.identifier)
        purpose = "verify_email" if user.email_verified_at is None else "signin_2fa" if user.two_factor_enabled else None
        try:
            async with session.begin():
                if auth.passwords.needs_rehash(user.password_hash):
                    user.password_hash = await auth.passwords.hash_async(password)
                if purpose is not None:
                    challenge_id, code = await issue(session, user_id=user.id, purpose=purpose, key=auth.otp_key, now=now)
                else:
                    await revoke_session(session, request.cookies.get(COOKIE_NAME))
                    token = await create_session(session, user_id=user.id, now=now)
        except OtpThrottledError as exc:
            raise AuthError("rate_limited", exc.retry_after_s) from None
    if purpose is not None:
        await auth.email.send(code_email(user.email, purpose, code))
        return CodeSent(challenge_id=challenge_id)
    set_session_cookie(response, token)
    return SignedIn(username=user.username)


@router.post("/otp/resend", status_code=202, response_model=CodeSent)
async def resend_code(body: ResendRequest, auth: AuthServices = Depends(auth_services)) -> CodeSent:
    async with auth.db() as session:
        try:
            async with session.begin():
                query = select(OtpChallenge.purpose, User).join(User, User.id == OtpChallenge.user_id)
                row = (await session.execute(query.where(OtpChallenge.id == body.challenge_id))).one_or_none()
                if row is None or row.purpose not in CODE_PURPOSES:
                    # unknown, such as the decoy for a taken email: same answer, nothing sent
                    return _decoy()
                challenge_id, code = await issue(session, user_id=row.User.id, purpose=row.purpose, key=auth.otp_key, now=auth.clock())
        except OtpThrottledError as exc:
            raise AuthError("rate_limited", exc.retry_after_s) from None
    await auth.email.send(code_email(row.User.email, row.purpose, code))
    return CodeSent(challenge_id=challenge_id)


@router.post("/password/forgot", status_code=202, response_model=CodeSent)
async def forgot_password(
    body: ForgotPasswordRequest, request: Request, background: BackgroundTasks, auth: AuthServices = Depends(auth_services)
) -> CodeSent:
    if not await auth.captcha.verify(body.captcha_token, action="reset", remote_ip=_ip(request)):
        raise AuthError("captcha_failed")
    async with auth.db() as session:
        try:
            async with session.begin():
                user = await find_user(session, body.email)
                if user is None:
                    return _decoy()
                challenge_id, code = await issue(session, user_id=user.id, purpose="reset_password", key=auth.otp_key, now=auth.clock())
        except OtpThrottledError:
            # the same answer as a sent code, a refusal would show that the account exists
            logger.info("password reset code throttled")
            return _decoy()
    background.add_task(_send_after_response, auth.email, code_email(user.email, "reset_password", code))
    return CodeSent(challenge_id=challenge_id)


@router.post("/password/reset", response_model=PasswordChanged)
async def reset_password(
    body: ResetPasswordRequest,
    response: Response,
    background: BackgroundTasks,
    auth: AuthServices = Depends(auth_services),
) -> PasswordChanged:
    now = auth.clock()
    password = body.password.get_secret_value()
    refused = False
    async with auth.db() as session:
        # one transaction: a password that is refused below rolls back the code too, so it still works
        async with session.begin():
            user_id = await verify(
                session, challenge_id=body.challenge_id, code=body.code, purpose="reset_password", key=auth.otp_key, now=now
            )
            if user_id is None:
                # nothing is raised here, ending the block commits the attempt count
                refused = True
            else:
                user = await session.get_one(User, user_id)
                # only a holder of the real code gets this far, so these answers tell nobody anything new
                if uses_personal_data(password, username=user.username, email=user.email):
                    raise AuthError("password_personal")
                breach = await auth.breach.check(password)
                if breach is BreachStatus.BREACHED:
                    raise AuthError("password_breached")
                if breach is BreachStatus.UNKNOWN:
                    logger.warning("breached password check unavailable, reset allowed without it")
                user.password_hash = await auth.passwords.hash_async(password)
                # the code went to this address, so using it proves the mailbox as well
                if user.email_verified_at is None:
                    user.email_verified_at = now
                await revoke_all(session, user_id=user.id)
                await retire_open(session, user_id=user.id, now=now)
                email = user.email
    if refused:
        raise AuthError("invalid_code")
    clear_session_cookie(response)
    background.add_task(_send_after_response, auth.email, password_changed_email(email))
    logger.info("password reset completed")
    return PasswordChanged()


@router.post("/signout")
async def signout(request: Request, response: Response, auth: AuthServices = Depends(auth_services)) -> dict[str, str]:
    async with auth.db() as session, session.begin():
        await revoke_session(session, request.cookies.get(COOKIE_NAME))
    clear_session_cookie(response)
    return {"status": "signed_out"}


@router.get("/me", response_model=Account)
async def me(user: User = Depends(current_user)) -> Account:
    return _account(user)


@router.post("/2fa", response_model=Account)
async def set_two_factor(
    body: TwoFactorRequest, request: Request, user: User = Depends(current_user), auth: AuthServices = Depends(auth_services)
) -> Account:
    client = client_key(_ip(request))
    if not await auth.passwords.verify_async(user.password_hash, body.password.get_secret_value()):
        auth.failed.record_failure(account=user.username_key, client=client)
        raise AuthError("invalid_credentials")
    async with auth.db() as session, session.begin():
        row = await session.get_one(User, user.id)
        row.two_factor_enabled = body.enabled
    logger.info("two-factor sign-in turned %s", "on" if body.enabled else "off")
    return _account(row)