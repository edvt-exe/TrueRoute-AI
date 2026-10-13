import asyncio
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

# all helpers are async-safe within one event loop, not thread-safe


class Spacer:
    def __init__(self, interval_s: float, clock: Clock, sleep: Sleep) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self._interval_s = interval_s
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last: float | None = None

    async def wait(self) -> None:
        # lock is held across the sleep so callers queue in order
        async with self._lock:
            now = self._clock()
            if self._last is not None:
                delay = self._last + self._interval_s - now
                if delay > 0:
                    await self._sleep(delay)
                    now = self._clock()
            self._last = now


class Cooldown:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._until = 0.0

    def trip(self, seconds: float) -> None:
        self._until = max(self._until, self._clock() + seconds)

    @property
    def active(self) -> bool:
        return self._clock() < self._until


class SlidingWindow:
    def __init__(self, limit: int, window_s: float, clock: Clock) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._hits: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._hits and self._hits[0] <= now - self._window_s:
            self._hits.popleft()

    def try_acquire(self) -> bool:
        # refuses instead of waiting, the caller decides what a refusal means
        now = self._clock()
        self._prune(now)
        if len(self._hits) >= self._limit:
            return False
        self._hits.append(now)
        return True

    def retry_after(self) -> float:
        now = self._clock()
        self._prune(now)
        if len(self._hits) < self._limit:
            return 0.0
        return self._hits[0] + self._window_s - now


@dataclass
class _Entry(Generic[V]):
    value: V
    expires_at: float


class TTLCache(Generic[K, V]):
    def __init__(self, max_entries: int, clock: Clock) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_entries = max_entries
        self._clock = clock
        self._data: OrderedDict[K, _Entry[V]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: K) -> tuple[bool, V | None]:
        entry = self._data.get(key)
        if entry is None:
            return False, None
        if self._clock() >= entry.expires_at:
            del self._data[key]
            return False, None
        self._data.move_to_end(key)
        return True, entry.value

    def put(self, key: K, value: V, ttl_s: float) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        self._data[key] = _Entry(value=value, expires_at=self._clock() + ttl_s)
        self._data.move_to_end(key)
        while len(self._data) > self._max_entries:
            self._data.popitem(last=False)


@dataclass
class Slot:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    # set by the holder so callers that waited can share the failure
    error: str | None = None


class KeyedLocks(Generic[K]):
    def __init__(self) -> None:
        self._slots: dict[K, Slot] = {}

    def __len__(self) -> int:
        return len(self._slots)

    @asynccontextmanager
    async def hold(self, key: K) -> AsyncIterator[Slot]:
        slot = self._slots.get(key)
        if slot is None:
            slot = Slot()
            self._slots[key] = slot
        slot.users += 1
        try:
            async with slot.lock:
                yield slot
        finally:
            slot.users -= 1
            # slot lives only while someone uses it, so failures are not sticky
            if slot.users == 0:
                del self._slots[key]