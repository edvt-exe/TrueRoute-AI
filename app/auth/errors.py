from fastapi import HTTPException

# every error the account routes return, frontend/auth.js keeps a message for each code
AUTH_ERRORS: dict[str, tuple[int, str]] = {
    "accounts_unavailable": (503, "accounts are not available on this server"),
    "not_signed_in": (401, "sign in first"),
    "captcha_required": (400, "complete the human check"),
    "captcha_failed": (400, "the human check failed, try again"),
    "password_breached": (400, "this password appears in known data breaches, choose another"),
    "username_taken": (409, "that username is taken"),
    "invalid_credentials": (401, "wrong username, email or password"),
    "invalid_code": (400, "that code is wrong or has expired"),
    "rate_limited": (429, "too many codes requested, wait and try again"),
}


class AuthError(HTTPException):
    # an http exception, so the app's handler turns it into the usual error body with this code
    def __init__(self, code: str, retry_after_s: int | None = None) -> None:
        status, message = AUTH_ERRORS[code]
        headers = {"Retry-After": str(retry_after_s)} if retry_after_s is not None else None
        super().__init__(status_code=status, detail=message, headers=headers)
        self.code = code