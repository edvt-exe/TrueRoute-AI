import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from app.utils.http_guard import (
    BodyTooLargeError,
    check_base_url,
    check_user_agent,
    read_capped,
)


def test_user_agent_ok_and_stripped() -> None:
    assert check_user_agent("  TrueRoute-AI/0.1 (+https://example.org) ") == (
        "TrueRoute-AI/0.1 (+https://example.org)"
    )


@pytest.mark.parametrize(
    "agent",
    [
        "",
        "  ",
        f"python-httpx/{httpx.__version__}",
        "python-requests/2.32",
        "Python-urllib/3.12",
        "curl/8.5.0",
        "Mozilla/5.0",
        "axios/1.7",
        "a\r\nb",
        "x" * 201,
    ],
)
def test_user_agent_rejected(agent: str) -> None:
    with pytest.raises(ValueError, match="user_agent"):
        check_user_agent(agent)


def test_base_url_normalized() -> None:
    assert check_base_url("https://overpass-api.de/api/") == "https://overpass-api.de/api"


@pytest.mark.parametrize(
    "url",
    ["http://x.org", "", "https://", "x.org", "https://x.org/?a=1", "https://u:p@x.org", "https://x.org/#f"],
)
def test_base_url_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        check_base_url(url)


def read(response: httpx.Response, cap: int) -> bytes:
    async def main() -> bytes:
        return await read_capped(response, cap)

    return asyncio.run(main())


def test_read_capped_ok_at_limit() -> None:
    assert read(httpx.Response(200, content=b"x" * 10), 10) == b"x" * 10


def test_read_capped_declared_too_large_is_not_read() -> None:
    consumed: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        consumed.append(1)
        yield b"x"

    response = httpx.Response(200, headers={"content-length": "11"}, content=body())
    with pytest.raises(BodyTooLargeError):
        read(response, 10)
    assert consumed == []


def test_read_capped_streamed_too_large() -> None:
    async def body() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b"x" * 5

    with pytest.raises(BodyTooLargeError):
        read(httpx.Response(200, content=body()), 10)