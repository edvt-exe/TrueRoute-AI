import httpx

MAX_USER_AGENT = 200

# stock library and browser agents do not identify the app, osm services reject them
STOCK_AGENT_PREFIXES = (
    "python-httpx",
    "httpx",
    "python-requests",
    "requests",
    "python-urllib",
    "urllib",
    "python/",
    "aiohttp",
    "curl/",
    "wget/",
    "mozilla/",
    "go-http-client",
    "okhttp",
    "java/",
    "node-fetch",
    "axios",
)


class BodyTooLargeError(Exception):
    pass


def check_user_agent(user_agent: str) -> str:
    value = user_agent.strip()
    if not value or len(value) > MAX_USER_AGENT or not value.isprintable():
        raise ValueError("user_agent must be a short printable string")
    if value.lower().startswith(STOCK_AGENT_PREFIXES):
        raise ValueError("user_agent must identify the application, not the http library")
    return value


def check_base_url(base_url: str) -> str:
    try:
        url = httpx.URL(base_url.strip())
    except httpx.InvalidURL:
        raise ValueError("base_url is not a valid url") from None
    if url.scheme != "https" or not url.host:
        raise ValueError("base_url must be an https url")
    if url.query or url.fragment or url.userinfo:
        raise ValueError("base_url must not contain a query, fragment or credentials")
    return str(url).rstrip("/")


async def read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    # refuse early on a declared size, then enforce while streaming
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise BodyTooLargeError()
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > max_bytes:
            raise BodyTooLargeError()
    return bytes(body)