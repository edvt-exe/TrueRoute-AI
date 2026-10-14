import asyncio
import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.profiles import RFC_9106_LOW_MEMORY

# rfc 9106 second recommended option: argon2id, 3 passes, 64 MiB, 4 lanes, 16 byte salt
DEFAULT_HASHER = PasswordHasher.from_parameters(RFC_9106_LOW_MEMORY)
# each hash takes 64 MiB, a burst of sign-ins must not exhaust memory
MAX_CONCURRENT_HASHES = 4
_DUMMY_PASSWORD = "trueroute-dummy-password-for-timing"


def normalize_password(password: str) -> str:
    # nist 800-63b: nfkc so the same password typed on any keyboard gives the same bytes
    return unicodedata.normalize("NFKC", password)


class Passwords:
    def __init__(self, hasher: PasswordHasher = DEFAULT_HASHER, *, max_concurrent: int = MAX_CONCURRENT_HASHES) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._hasher = hasher
        self._slots = asyncio.Semaphore(max_concurrent)
        # checked when the account does not exist, so the response time does not reveal it
        self._dummy = hasher.hash(_DUMMY_PASSWORD)

    def hash(self, password: str) -> str:
        # argon2-cffi draws a fresh random salt for every hash
        return self._hasher.hash(normalize_password(password))

    def verify(self, stored: str | None, password: str) -> bool:
        target = self._dummy if stored is None else stored
        try:
            self._hasher.verify(target, normalize_password(password))
        except (VerificationError, InvalidHashError):
            return False
        return stored is not None

    def needs_rehash(self, stored: str) -> bool:
        # true when the stored hash used weaker parameters than the current ones
        return self._hasher.check_needs_rehash(stored)

    # argon2 is cpu bound, these keep it off the event loop
    async def hash_async(self, password: str) -> str:
        async with self._slots:
            return await asyncio.to_thread(self.hash, password)

    async def verify_async(self, stored: str | None, password: str) -> bool:
        async with self._slots:
            return await asyncio.to_thread(self.verify, stored, password)