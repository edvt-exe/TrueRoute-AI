import asyncio
import inspect
import json
import logging
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

import anthropic
import httpx2
import pytest

from app.schemas import Coordinates, Place, PlaceCandidates, PlaceCategory, UserRequest
from app.services.llm_agent import (
    ALLOWED_EFFORTS,
    ATTEMPT_TIMEOUT_S,
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    MAX_ATTEMPTS,
    MAX_TOKENS,
    SDK_MAX_RETRIES,
    TOTAL_TIMEOUT_S,
    AgentResult,
    LLMAgent,
    LLMConfigError,
    LLMError,
    LLMInvalidOutputError,
    LLMRefusedError,
    LLMTruncatedError,
    LLMUnavailableError,
    NotEnoughPlacesError,
)
from app.utils.prompts import build_output_schema, build_system_prompt, build_user_prompt

# fake key with a marker so any leak into logs or errors is caught
KEY = "sk-ant-test-SECRET-MARKER"
MARKER = "SECRET-MARKER"
CENTER = Coordinates(lat=44.436, lon=26.103)
CANDS = PlaceCandidates(
    center=CENTER,
    radius_m=7718,
    places=[
        Place(
            ref=f"p{n}",
            osm_type="node",
            osm_id=n,
            name=f"Place {n}",
            category=PlaceCategory.RESTAURANT if n == 5 else PlaceCategory.ATTRACTION,
            coords=Coordinates(lat=44.43 + n * 0.001, lon=26.1),
        )
        for n in range(1, 6)
    ],
)
REQ = UserRequest(city="Bucharest", days=2, pace="moderate")
GOOD = {
    "days": [
        {"day": 1, "place_refs": ["p1", "p2", "p5"], "theme": "Old town"},
        {"day": 2, "place_refs": ["p3", "p4"], "theme": "Parks"},
    ]
}

def slots(plan: dict[str, Any]) -> dict[str, Any]:
    # tests describe plans as ref lists, the model answers in numbered slots
    return {
        "days": {
            f"day{d['day']}": {"stops": {f"s{i}": ref for i, ref in enumerate(d["place_refs"], start=1)}, "theme": d["theme"]}
            for d in plan["days"]
        }
    }


Responder = Callable[[httpx2.Request], httpx2.Response | Awaitable[httpx2.Response]]


def message(
    payload: Any = None,
    stop: str = "end_turn",
    tokens: tuple[int, int] = (1000, 200),
    thinking: bool = False,
    request_id: str = "req_1",
) -> httpx2.Response:
    text = payload if isinstance(payload, str) else json.dumps(slots(GOOD if payload is None else payload))
    content: list[dict[str, Any]] = []
    if thinking:
        content.append({"type": "thinking", "thinking": "grouping by area", "signature": "sig"})
    content.append({"type": "text", "text": text})
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": DEFAULT_MODEL,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
    }
    return httpx2.Response(200, json=body, headers={"request-id": request_id})


def api_error(status: int, kind: str, text: str = "error") -> httpx2.Response:
    body = {"type": "error", "error": {"type": kind, "message": text}, "request_id": "req_err"}
    return httpx2.Response(status, json=body, headers={"request-id": "req_err", "retry-after-ms": "1"})


class FakeAPI:
    def __init__(self, *responses: httpx2.Response | Responder) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, httpx2.Response):
            return item
        result = item(request)
        if inspect.isawaitable(result):
            result = await result
        return result

    @property
    def calls(self) -> int:
        return len(self.requests)

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def run(api: FakeAPI, request: UserRequest = REQ, cands: PlaceCandidates = CANDS, **kwargs: Any) -> Any:
    async def main() -> Any:
        transport = httpx2.MockTransport(api)
        async with AsyncClient(transport) as client:
            agent = LLMAgent(client, **kwargs)
            try:
                return await agent.plan(request, cands)
            except LLMError as exc:
                return exc

    return asyncio.run(main())


def AsyncClient(transport: httpx2.MockTransport) -> anthropic.AsyncAnthropic:
    http = anthropic.DefaultAsyncHttpxClient(transport=transport)
    return anthropic.AsyncAnthropic(api_key=KEY, http_client=http)


# happy path and request shape


def test_happy_path() -> None:
    api = FakeAPI(message(request_id="req_ok"))
    res = run(api)
    assert isinstance(res, AgentResult)
    assert [[s.place.name for s in d.stops] for d in res.itinerary.day_plans] == [
        ["Place 1", "Place 2", "Place 5"],
        ["Place 3", "Place 4"],
    ]
    assert res.usage.model == DEFAULT_MODEL
    assert (res.usage.attempts, res.usage.input_tokens, res.usage.output_tokens) == (1, 1000, 200)
    assert res.usage.request_ids == ("req_ok",)
    assert res.usage.latency_s >= 0
    assert api.calls == 1


def test_request_body() -> None:
    api = FakeAPI(message())
    run(api)
    body = api.body()
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 8000
    assert body["system"] == build_system_prompt()
    assert body["messages"] == [{"role": "user", "content": build_user_prompt(REQ, CANDS)}]
    assert body["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": build_output_schema(REQ, CANDS)},
    }
    assert "thinking" not in body
    assert set(body) == {"model", "max_tokens", "system", "messages", "output_config"}


def test_request_headers() -> None:
    api = FakeAPI(message())
    run(api)
    req = api.requests[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/messages"
    assert req.headers["x-api-key"] == KEY


def test_model_and_effort_are_configurable() -> None:
    api = FakeAPI(message())
    res = run(api, model="claude-haiku-5-5", effort="low")
    assert api.body()["model"] == "claude-haiku-5-5"
    assert api.body()["output_config"]["effort"] == "low"
    assert res.usage.model == "claude-haiku-5-5"


def test_thinking_block_is_ignored() -> None:
    res = run(FakeAPI(message(thinking=True)))
    assert isinstance(res, AgentResult)


def test_answer_split_across_text_blocks() -> None:
    text = json.dumps(slots(GOOD))
    body = json.loads(message().content)
    body["content"] = [{"type": "text", "text": text[:20]}, {"type": "text", "text": text[20:]}]
    res = run(FakeAPI(httpx2.Response(200, json=body)))
    assert isinstance(res, AgentResult)


# stop reasons


def test_refusal_is_not_retried() -> None:
    api = FakeAPI(message("I can't help with that.", stop="refusal"))
    res = run(api)
    assert isinstance(res, LLMRefusedError)
    assert api.calls == 1


@pytest.mark.parametrize("stop", ["max_tokens", "model_context_window_exceeded"])
def test_truncation_is_not_retried(stop: str) -> None:
    api = FakeAPI(message('{"days":[{"day":1,', stop=stop))
    res = run(api)
    assert isinstance(res, LLMTruncatedError)
    assert api.calls == 1


def test_unexpected_stop_reason() -> None:
    api = FakeAPI(message(stop="tool_use"))
    res = run(api)
    assert isinstance(res, LLMInvalidOutputError)
    assert api.calls == 1


# self-correction loop


def test_invalid_then_valid_retries_once_with_feedback() -> None:
    over_cap = {"days": [{"day": 1, "place_refs": ["p1", "p2", "p3", "p4", "p5"], "theme": "All"}]}
    fixed = {"days": [{"day": 1, "place_refs": ["p1", "p2", "p5"], "theme": "Old town"}]}
    api = FakeAPI(message(over_cap, tokens=(1000, 300), request_id="req_a"), message(fixed, tokens=(1100, 250), request_id="req_b"))
    res = run(api, request=REQ.model_copy(update={"days": 1}))
    assert isinstance(res, AgentResult)
    assert api.calls == 2
    first = api.body(0)["messages"][0]["content"]
    second = api.body(1)["messages"][0]["content"]
    assert second.startswith(first)
    feedback = second[len(first) :]
    # the 5th stop has no slot, so the answer cannot even be read as a plan
    assert "A previous answer was rejected: model output does not match the slot layout: day1.stops" in feedback
    assert feedback.endswith("Follow the rules and the schema exactly.")
    assert (res.usage.attempts, res.usage.input_tokens, res.usage.output_tokens) == (2, 2100, 550)
    assert res.usage.request_ids == ("req_a", "req_b")


def test_feedback_carries_schema_detail() -> None:
    api = FakeAPI(message("not json at all"), message())
    res = run(api)
    assert isinstance(res, AgentResult)
    second = api.body(1)["messages"][0]["content"]
    assert "A previous answer was rejected: invalid model output: Invalid JSON" in second
    # raw model output never goes back into the prompt
    assert "not json at all" not in second


def test_unknown_ref_triggers_retry() -> None:
    bad = {"days": [{"day": 1, "place_refs": ["p25", "p1"], "theme": "x"}, {"day": 2, "place_refs": ["p2", "p3"], "theme": "y"}]}
    api = FakeAPI(message(bad), message())
    res = run(api)
    assert isinstance(res, AgentResult)
    assert "unknown place ref p25" in api.body(1)["messages"][0]["content"]


def test_invalid_twice_fails() -> None:
    dup = {"days": [{"day": 1, "place_refs": ["p1", "p2"], "theme": "x"}, {"day": 2, "place_refs": ["p1", "p3"], "theme": "y"}]}
    api = FakeAPI(message(dup))
    res = run(api)
    assert isinstance(res, LLMInvalidOutputError)
    assert res.message == "model output failed validation twice"
    assert api.calls == MAX_ATTEMPTS == 2


def test_refusal_on_retry_is_reported() -> None:
    api = FakeAPI(message("nope"), message("I can't help.", stop="refusal"))
    res = run(api)
    assert isinstance(res, LLMRefusedError)
    assert api.calls == 2


# pre-check


def test_old_array_format_is_rejected() -> None:
    old = json.dumps({"days": [{"day": 1, "place_refs": ["p1", "p2"], "theme": "x"}]})
    api = FakeAPI(message(old))
    res = run(api)
    assert isinstance(res, LLMInvalidOutputError)
    assert "does not match the slot layout" in api.body(1)["messages"][0]["content"]


def test_request_uses_slot_schema() -> None:
    api = FakeAPI(message())
    run(api)
    schema = api.body()["output_config"]["format"]["schema"]
    days = schema["properties"]["days"]
    assert days["required"] == ["day1", "day2"]
    # five places for two days: two required slots, four in total
    stops = days["properties"]["day1"]["properties"]["stops"]
    assert stops["required"] == ["s1", "s2"]
    assert list(stops["properties"]) == ["s1", "s2", "s3", "s4"]


def test_not_enough_places_makes_no_call() -> None:
    api = FakeAPI(message())
    res = run(api, request=REQ.model_copy(update={"days": 6}))
    assert isinstance(res, NotEnoughPlacesError)
    assert api.calls == 0


def test_exactly_enough_places_is_allowed() -> None:
    plan = {"days": [{"day": d, "place_refs": [f"p{d}"], "theme": "t"} for d in range(1, 6)]}
    res = run(FakeAPI(message(plan)), request=REQ.model_copy(update={"days": 5}))
    assert isinstance(res, AgentResult)


# api errors


@pytest.mark.parametrize(
    ("status", "kind", "expected", "text"),
    [
        (401, "authentication_error", LLMConfigError, "check ANTHROPIC_API_KEY"),
        (403, "permission_error", LLMConfigError, "check ANTHROPIC_API_KEY"),
        (400, "invalid_request_error", LLMConfigError, "rejected the request"),
        (429, "rate_limit_error", LLMUnavailableError, "rate limited"),
        (500, "api_error", LLMUnavailableError, "http 500"),
        (529, "overloaded_error", LLMUnavailableError, "http 529"),
    ],
)
def test_api_errors(status: int, kind: str, expected: type, text: str) -> None:
    api = FakeAPI(api_error(status, kind))
    res = run(api, sdk_max_retries=0)
    assert isinstance(res, expected)
    assert text in res.message
    assert api.calls == 1


def test_sdk_retries_transient_errors() -> None:
    api = FakeAPI(api_error(500, "api_error"), api_error(529, "overloaded_error"), message())
    res = run(api)
    assert isinstance(res, AgentResult)
    assert api.calls == 3


def test_sdk_retry_budget_is_bounded() -> None:
    api = FakeAPI(api_error(500, "api_error"))
    res = run(api)
    assert isinstance(res, LLMUnavailableError)
    assert api.calls == 1 + SDK_MAX_RETRIES


def test_auth_error_not_retried_by_sdk() -> None:
    api = FakeAPI(api_error(401, "authentication_error"))
    run(api)
    assert api.calls == 1


def test_connection_error() -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down", request=request)

    api = FakeAPI(down)
    res = run(api, sdk_max_retries=0)
    assert isinstance(res, LLMUnavailableError)
    assert res.message == "could not reach the model api"


def test_attempt_timeout() -> None:
    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    res = run(FakeAPI(slow), sdk_max_retries=0)
    assert isinstance(res, LLMUnavailableError)
    assert res.message == "model request timed out"


def test_total_timeout_bounds_everything() -> None:
    async def hang(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return message()

    async def timed() -> tuple[Any, float]:
        loop = asyncio.get_running_loop()
        start = loop.time()
        async with AsyncClient(httpx2.MockTransport(FakeAPI(hang))) as client:
            try:
                await LLMAgent(client, total_timeout_s=0.05).plan(REQ, CANDS)
            except LLMError as exc:
                return exc, loop.time() - start
        raise AssertionError("expected a timeout")

    exc, elapsed = asyncio.run(timed())
    assert isinstance(exc, LLMUnavailableError)
    assert exc.message == "model timed out"
    assert elapsed < 1.0


# secrets and safety


def test_no_secret_or_body_in_errors_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    # the key never shows up, even with the sdk logging at debug
    caplog.set_level(logging.DEBUG)
    for status, kind in ((401, "authentication_error"), (400, "invalid_request_error"), (500, "api_error")):
        res = run(FakeAPI(api_error(status, kind, f"bad key {KEY}")), sdk_max_retries=0)
        assert MARKER not in res.message
        assert res.__cause__ is None
        assert res.__suppress_context__
        assert MARKER not in "".join(traceback.format_exception(res))
    run(FakeAPI(message("not json"), message()))
    assert MARKER not in caplog.text
    # the sdk logs request bodies at debug, so main.py keeps it at warning
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app."))
    assert "llm attempt" in ours
    assert "not json" not in ours
    assert "Place 1" not in ours
    assert "Bucharest" not in ours


def test_logs_usage_without_content(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.services.llm_agent")
    run(FakeAPI(message(request_id="req_log")))
    assert "llm attempt 1 model claude-sonnet-5-5 stop end_turn tokens in 1000 out 200 request req_log" in caplog.text


# constructor and limits


@pytest.mark.parametrize("model", ["", "Claude Sonnet", "claude sonnet", "x", "a" * 101, "claude/../x"])
def test_bad_model_rejected(model: str) -> None:
    with pytest.raises(ValueError, match="model"):
        LLMAgent(anthropic.AsyncAnthropic(api_key=KEY), model=model)


@pytest.mark.parametrize("effort", ["max", "xhigh", "adaptive", "", "MEDIUM"])
def test_bad_effort_rejected(effort: str) -> None:
    with pytest.raises(ValueError, match="effort"):
        LLMAgent(anthropic.AsyncAnthropic(api_key=KEY), effort=effort)


@pytest.mark.parametrize(
    "kwargs", [{"sdk_max_retries": -1}, {"attempt_timeout_s": 0}, {"total_timeout_s": -1}]
)
def test_bad_limits_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        LLMAgent(anthropic.AsyncAnthropic(api_key=KEY), **kwargs)


def test_client_options_are_bounded() -> None:
    client = anthropic.AsyncAnthropic(api_key=KEY, timeout=600, max_retries=9)
    agent = LLMAgent(client)
    assert agent._client.timeout == ATTEMPT_TIMEOUT_S
    assert agent._client.max_retries == SDK_MAX_RETRIES


def test_spec_values() -> None:
    # values agreed in the design, change only on purpose
    assert (DEFAULT_MODEL, DEFAULT_EFFORT) == ("claude-sonnet-5-5", "medium")
    assert ALLOWED_EFFORTS == ("low", "medium", "high")
    assert (MAX_TOKENS, MAX_ATTEMPTS) == (8000, 2)
    assert (ATTEMPT_TIMEOUT_S, SDK_MAX_RETRIES, TOTAL_TIMEOUT_S) == (60, 2, 150)