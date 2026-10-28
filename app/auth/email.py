import sys
from dataclasses import dataclass
from typing import Protocol, TextIO

from app.auth.otp import TTL

SUBJECTS = {
    "verify_email": "Confirm your TrueRoute email",
    "signin_2fa": "Your TrueRoute sign-in code",
    "reset_password": "Reset your TrueRoute password",
}
ACTIONS = {
    "verify_email": "confirm your email address",
    "signin_2fa": "finish signing in",
    "reset_password": "reset your password",
}
# what to do when the code was not expected, different for each purpose
UNEXPECTED = {
    "verify_email": "If you did not create a TrueRoute account, ignore this email.",
    "signin_2fa": "If you are not signing in right now, someone knows your password. Change it as soon as you can.",
    "reset_password": "If you did not ask to reset your password, ignore this email. Your password stays the same.",
}


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text: str

    def __post_init__(self) -> None:
        # a line break in a header would let the text add headers such as bcc
        if any(ch in value for value in (self.to, self.subject) for ch in "\r\n"):
            raise ValueError("email headers must be a single line")


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> None: ...


class ConsoleEmailSender:
    # development only: the printed text holds live codes, never use it on a server
    def __init__(self, stream: TextIO = sys.stderr) -> None:
        self._stream = stream

    async def send(self, message: EmailMessage) -> None:
        self._stream.write(
            "\n----- email, development only, not sent -----\n"
            f"To: {message.to}\nSubject: {message.subject}\n\n{message.text}\n"
            "----------------------------------------------\n"
        )
        self._stream.flush()


def code_email(to: str, purpose: str, code: str) -> EmailMessage:
    minutes = int(TTL.total_seconds() // 60)
    text = (
        f"Your TrueRoute code is {code}\n\n"
        f"Enter it to {ACTIONS[purpose]}. It expires in {minutes} minutes and works once.\n\n"
        f"{UNEXPECTED[purpose]}\n"
        "TrueRoute will never ask you for this code by phone or chat."
    )
    return EmailMessage(to=to, subject=SUBJECTS[purpose], text=text)


def password_changed_email(to: str) -> EmailMessage:
    # the owner hears about every change, a reset nobody asked for is visible even if the code was stolen
    text = (
        "The password of your TrueRoute account was just changed, and every device was signed out.\n\n"
        "If that was you, there is nothing to do.\n"
        "If it was not you, reset your password again right away and check who else can read this mailbox."
    )
    return EmailMessage(to=to, subject="Your TrueRoute password was changed", text=text)


def existing_account_email(to: str) -> EmailMessage:
    # sign-up answers the same for a taken email, the real owner learns about the attempt here
    text = (
        "Someone just tried to create a TrueRoute account with this email address, "
        "but it already has an account.\n\n"
        "If that was you, sign in instead, or reset your password if you forgot it.\n"
        "If it was not you, you can ignore this email. Your account has not changed."
    )
    return EmailMessage(to=to, subject="Sign-up attempt with your email", text=text)