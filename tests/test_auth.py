import asyncio

import httpx
import pytest
from argon2 import PasswordHasher, Type
from pydantic import ValidationError

from app.auth.breach import BreachChecker, BreachStatus
from app.auth.passwords import DEFAULT_HASHER, Passwords
from app.auth.schemas import OtpVerifyRequest, SignInRequest, SignUpRequest

PW = "Walk-the-Seine-42"
UA = "TrueRoute-AI-tests/0.1 (+https://example.org/trueroute)"
# sha-1 of "password" is 5BAA6 + 1E4C9B93F3F0682250B6CF8331B7EE68FD8
SUFFIX = "1E4C9B93F3F0682250B6CF8331B7EE68FD8"
# minimum argon2 cost so the suite stays fast, the real parameters have their own test
FAST = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


def signup(**changes: object) -> dict[str, object]:
    data: dict[str, object] = {
        "username": "Eduard_V",
        "email": "Eduard@Example.com",
        "password": PW,
        "password_confirm": PW,
        "captcha_token": "XXXX.DUMMY.TOKEN",
    }
    data.update(changes)
    return data


def signup_error(password: str, **changes: object) -> str:
    with pytest.raises(ValidationError) as exc:
        SignUpRequest.model_validate(signup(password=password, password_confirm=password, **changes))
    return str(exc.value)


def breach_check(answer: httpx.Response | Exception, password: str = "password") -> tuple[BreachStatus, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    checker = BreachChecker(httpx.AsyncClient(transport=httpx.MockTransport(handler)), UA)
    return asyncio.run(checker.check(password)), seen


# argon2 utility


def test_default_hasher_is_argon2id_rfc_9106() -> None:
    assert DEFAULT_HASHER.type is Type.ID
    assert (DEFAULT_HASHER.time_cost, DEFAULT_HASHER.memory_cost, DEFAULT_HASHER.parallelism) == (3, 65536, 4)


def test_hash_and_verify_round_trip() -> None:
    passwords = Passwords(FAST)
    stored = passwords.hash(PW)
    assert stored.startswith("$argon2id$")
    assert passwords.verify(stored, PW) is True


def test_wrong_password_fails() -> None:
    passwords = Passwords(FAST)
    assert passwords.verify(passwords.hash(PW), "Walk-the-Seine-43") is False


def test_hash_is_salted_and_hides_the_password() -> None:
    passwords = Passwords(FAST)
    first, second = passwords.hash(PW), passwords.hash(PW)
    assert first != second
    assert PW not in first


def test_malformed_hash_fails_closed() -> None:
    assert Passwords(FAST).verify("not-a-hash", PW) is False


def test_unknown_account_costs_a_real_hash() -> None:
    checked: list[str] = []

    class Spy(PasswordHasher):
        def verify(self, hash: str | bytes, password: str | bytes) -> bool:
            checked.append(str(hash))
            return super().verify(hash, password)

    passwords = Passwords(Spy(time_cost=1, memory_cost=8, parallelism=1))
    assert passwords.verify(None, PW) is False
    # same work as a real account, so timing does not reveal which accounts exist
    assert len(checked) == 1 and checked[0].startswith("$argon2id$")
    # the internal dummy password must never open a missing account
    assert passwords.verify(None, "trueroute-dummy-password-for-timing") is False


def test_unicode_variants_verify() -> None:
    # a decomposed accent and full width digits, as some keyboards type them
    passwords = Passwords(FAST)
    assert passwords.verify(passwords.hash("Café-Paris-2026"), "Cafe\u0301-Paris-\uff12\uff10\uff12\uff16") is True


def test_async_wrappers() -> None:
    passwords = Passwords(FAST)

    async def run() -> bool:
        return await passwords.verify_async(await passwords.hash_async(PW), PW)

    assert asyncio.run(run()) is True


# sign-up validator


def test_valid_signup() -> None:
    req = SignUpRequest.model_validate(signup())
    assert req.username_key == "eduard_v"
    assert req.email == "eduard@example.com"
    assert PW not in repr(req)


def test_minimum_length_boundary() -> None:
    assert "at least 12 characters" in signup_error("Walk-Seine1")
    assert SignUpRequest.model_validate(signup(password="Walk-Seine-1", password_confirm="Walk-Seine-1"))


def test_missing_uppercase() -> None:
    # only the uppercase letter is missing, so only that is reported
    text = signup_error("walk-the-seine-42")
    assert "must include an uppercase letter" in text
    assert text.count("must include") == 1


def test_missing_symbol() -> None:
    text = signup_error("WalkTheSeine42")
    assert "must include a symbol" in text
    assert text.count("must include") == 1


def test_password_with_username_or_email_fails() -> None:
    # letter case differs on purpose, the check ignores it
    assert "must not contain your username or email" in signup_error("eDUARD_v-Walks-42", email="ana@example.com")
    assert "must not contain your username or email" in signup_error("x-EVOICU-2026-y", email="evoicu@example.com")


def test_invisible_characters_fail() -> None:
    assert "line breaks or invisible characters" in signup_error("Walk-the\u200bSeine-42")


def test_mismatched_confirmation() -> None:
    with pytest.raises(ValidationError) as exc:
        SignUpRequest.model_validate(signup(password_confirm="Walk-the-Seine-43"))
    assert "passwords do not match" in str(exc.value)


def test_errors_list_every_problem_without_the_password() -> None:
    text = signup_error("walkingparis")
    assert "must include a digit" in text and "must include a symbol" in text
    assert "walkingparis" not in text
    assert "input_value" not in text


def test_reserved_username() -> None:
    with pytest.raises(ValidationError):
        SignUpRequest.model_validate(signup(username="Admin"))


def test_username_with_trailing_newline() -> None:
    # regression: a plain $ in the pattern once let this through
    with pytest.raises(ValidationError):
        SignUpRequest.model_validate(signup(username="eduard\n"))


def test_invalid_email() -> None:
    with pytest.raises(ValidationError):
        SignUpRequest.model_validate(signup(email="eduard@"))


def test_unknown_fields_refused() -> None:
    with pytest.raises(ValidationError):
        SignUpRequest.model_validate(signup(is_admin=True))


# sign-in and otp validators


def test_signin_skips_the_policy() -> None:
    # accounts made under an older policy must still sign in
    assert SignInRequest.model_validate({"identifier": "eduard", "password": "old"}).captcha_token is None


def test_otp_code_must_be_six_digits() -> None:
    assert OtpVerifyRequest.model_validate({"challenge_id": "a" * 22, "code": "012345"}).code == "012345"
    with pytest.raises(ValidationError):
        OtpVerifyRequest.model_validate({"challenge_id": "a" * 22, "code": "12345a"})


# breached passwords


def test_breached_password_is_detected() -> None:
    status, seen = breach_check(httpx.Response(200, content=f"{SUFFIX}:10434004\r\n".encode()))
    assert status is BreachStatus.BREACHED
    # k-anonymity: only the 5 character prefix leaves the server
    assert str(seen[0].url) == "https://api.pwnedpasswords.com/range/5BAA6"
    assert seen[0].headers["Add-Padding"] == "true"


def test_padding_entry_is_not_a_breach() -> None:
    status, _ = breach_check(httpx.Response(200, content=f"{SUFFIX}:0\r\n".encode()))
    assert status is BreachStatus.CLEAN


def test_breach_redirect_is_not_followed() -> None:
    # a redirect could point at a server that answers whatever it likes
    status, seen = breach_check(httpx.Response(302, headers={"Location": "https://evil.example/range"}))
    assert status is BreachStatus.UNKNOWN
    assert len(seen) == 1


def test_breach_api_down_is_unknown() -> None:
    status, _ = breach_check(httpx.ConnectError("down"))
    assert status is BreachStatus.UNKNOWN