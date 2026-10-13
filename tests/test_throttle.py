import asyncio

import pytest

from app.utils.throttle import Cooldown, KeyedLocks, SlidingWindow, Spacer, TTLCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay
        await asyncio.sleep(0)


# spacer


def test_spacer_first_call_does_not_wait() -> None:
    clock = FakeClock()

    async def main() -> None:
        await Spacer(2.0, clock, clock.sleep).wait()

    asyncio.run(main())
    assert clock.sleeps == []


def test_spacer_waits_remaining_interval() -> None:
    clock = FakeClock()

    async def main() -> None:
        spacer = Spacer(2.0, clock, clock.sleep)
        await spacer.wait()
        clock.now += 0.5
        await spacer.wait()

    asyncio.run(main())
    assert clock.sleeps == [pytest.approx(1.5)]


def test_spacer_concurrent_callers_queue() -> None:
    clock = FakeClock()
    starts: list[float] = []

    async def one(spacer: Spacer) -> None:
        await spacer.wait()
        starts.append(clock())

    async def main() -> None:
        spacer = Spacer(2.0, clock, clock.sleep)
        await asyncio.gather(*(one(spacer) for _ in range(4)))

    asyncio.run(main())
    assert starts == [pytest.approx(v) for v in (1000.0, 1002.0, 1004.0, 1006.0)]


def test_spacer_rejects_bad_interval() -> None:
    clock = FakeClock()
    with pytest.raises(ValueError):
        Spacer(0, clock, clock.sleep)


# cooldown


def test_cooldown_trip_and_expire() -> None:
    clock = FakeClock()
    cd = Cooldown(clock)
    assert not cd.active
    cd.trip(30)
    assert cd.active
    clock.now += 29.9
    assert cd.active
    clock.now += 0.1
    assert not cd.active


def test_cooldown_never_shortens() -> None:
    clock = FakeClock()
    cd = Cooldown(clock)
    cd.trip(60)
    cd.trip(10)
    clock.now += 30
    assert cd.active


# ttl cache


def test_cache_get_put_and_ttl() -> None:
    clock = FakeClock()
    cache: TTLCache[str, int] = TTLCache(4, clock)
    assert cache.get("a") == (False, None)
    cache.put("a", 1, 10)
    assert cache.get("a") == (True, 1)
    clock.now += 9.99
    assert cache.get("a") == (True, 1)
    clock.now += 0.01
    assert cache.get("a") == (False, None)
    assert len(cache) == 0


def test_cache_stores_none_as_a_value() -> None:
    clock = FakeClock()
    cache: TTLCache[str, int | None] = TTLCache(4, clock)
    cache.put("a", None, 10)
    assert cache.get("a") == (True, None)


def test_cache_lru_eviction() -> None:
    clock = FakeClock()
    cache: TTLCache[str, int] = TTLCache(2, clock)
    cache.put("a", 1, 10)
    cache.put("b", 2, 10)
    cache.get("a")
    cache.put("c", 3, 10)
    assert cache.get("b") == (False, None)
    assert cache.get("a") == (True, 1)
    assert cache.get("c") == (True, 3)
    assert len(cache) == 2


def test_cache_overwrite_refreshes() -> None:
    clock = FakeClock()
    cache: TTLCache[str, int] = TTLCache(2, clock)
    cache.put("a", 1, 10)
    cache.put("b", 2, 10)
    cache.put("a", 9, 10)
    cache.put("c", 3, 10)
    assert cache.get("a") == (True, 9)
    assert cache.get("b") == (False, None)


@pytest.mark.parametrize("ttl", [0, -1])
def test_cache_rejects_bad_ttl(ttl: float) -> None:
    with pytest.raises(ValueError):
        TTLCache[str, int](2, FakeClock()).put("a", 1, ttl)


def test_cache_rejects_bad_size() -> None:
    with pytest.raises(ValueError):
        TTLCache[str, int](0, FakeClock())


# keyed locks


def test_keyed_locks_serialize_same_key() -> None:
    events: list[str] = []

    async def worker(locks: KeyedLocks[str], name: str) -> None:
        async with locks.hold("k"):
            events.append(f"{name}-in")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            events.append(f"{name}-out")

    async def main() -> int:
        locks: KeyedLocks[str] = KeyedLocks()
        await asyncio.gather(worker(locks, "a"), worker(locks, "b"))
        return len(locks)

    assert asyncio.run(main()) == 0
    assert events == ["a-in", "a-out", "b-in", "b-out"]


def test_keyed_locks_different_keys_overlap() -> None:
    events: list[str] = []

    async def worker(locks: KeyedLocks[str], key: str) -> None:
        async with locks.hold(key):
            events.append(f"{key}-in")
            await asyncio.sleep(0)
            events.append(f"{key}-out")

    async def main() -> None:
        locks: KeyedLocks[str] = KeyedLocks()
        await asyncio.gather(worker(locks, "a"), worker(locks, "b"))

    asyncio.run(main())
    assert events[:2] == ["a-in", "b-in"]


def test_keyed_locks_share_error_then_forget() -> None:
    seen: list[str | None] = []

    async def first(locks: KeyedLocks[str]) -> None:
        async with locks.hold("k") as slot:
            await asyncio.sleep(0)
            slot.error = "boom"

    async def waiter(locks: KeyedLocks[str]) -> None:
        async with locks.hold("k") as slot:
            seen.append(slot.error)

    async def main() -> None:
        locks: KeyedLocks[str] = KeyedLocks()
        await asyncio.gather(first(locks), waiter(locks))
        assert len(locks) == 0
        await waiter(locks)

    asyncio.run(main())
    assert seen == ["boom", None]


def test_keyed_locks_release_on_exception() -> None:
    async def main() -> int:
        locks: KeyedLocks[str] = KeyedLocks()
        with pytest.raises(RuntimeError):
            async with locks.hold("k"):
                raise RuntimeError("x")
        async with locks.hold("k"):
            pass
        return len(locks)

    assert asyncio.run(main()) == 0

# sliding window


def test_window_allows_up_to_limit() -> None:
    clock = FakeClock()
    win = SlidingWindow(3, 60.0, clock)
    assert [win.try_acquire() for _ in range(4)] == [True, True, True, False]


def test_window_refusal_is_not_counted() -> None:
    clock = FakeClock()
    win = SlidingWindow(1, 60.0, clock)
    assert win.try_acquire()
    for _ in range(5):
        assert not win.try_acquire()
    clock.now += 60.0
    assert win.try_acquire()


def test_window_slides() -> None:
    clock = FakeClock()
    win = SlidingWindow(2, 60.0, clock)
    assert win.try_acquire()
    clock.now += 30.0
    assert win.try_acquire()
    clock.now += 29.9
    assert not win.try_acquire()
    # the first hit leaves the window exactly 60 s after it was taken
    clock.now += 0.1
    assert win.try_acquire()
    assert not win.try_acquire()


def test_window_retry_after() -> None:
    clock = FakeClock()
    win = SlidingWindow(2, 60.0, clock)
    assert win.retry_after() == 0.0
    win.try_acquire()
    clock.now += 10.0
    win.try_acquire()
    assert win.retry_after() == pytest.approx(50.0)
    clock.now += 50.0
    assert win.retry_after() == 0.0


@pytest.mark.parametrize(("limit", "window"), [(0, 60.0), (1, 0.0), (1, -1.0)])
def test_window_rejects_bad_args(limit: int, window: float) -> None:
    with pytest.raises(ValueError):
        SlidingWindow(limit, window, FakeClock())