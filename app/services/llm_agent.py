import asyncio
import logging
import re
import time
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel, ConfigDict, Field

from app.schemas import (
    Itinerary,
    ItineraryValidationError,
    PlaceCandidates,
    UserRequest,
    resolve,
)
from app.utils.prompts import (
    build_output_schema,
    build_system_prompt,
    build_user_prompt,
    parse_slot_answer,
    slot_layout,
)
from app.utils.throttle import Clock

logger = logging.getLogger(__name__)

# docs: sonnet 5.5 is the best mix of speed and intelligence, swap via config after the a/b check
DEFAULT_MODEL = "claude-sonnet-5-5"
# assumption: medium keeps latency and cost down, docs default for sonnet 5.5 is high
DEFAULT_EFFORT = "medium"
# xhigh and max are left out on purpose, they cost a lot for this small task
ALLOWED_EFFORTS = ("low", "medium", "high")
# thinking counts toward max_tokens, the json answer itself is a few hundred tokens
MAX_TOKENS = 8000
# assumption: bounds from the design, tune with real latency data
ATTEMPT_TIMEOUT_S = 60.0
SDK_MAX_RETRIES = 2
TOTAL_TIMEOUT_S = 150.0
# first answer plus one self-correction
MAX_ATTEMPTS = 2

_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{1,99}$")
_TRUNCATED = ("max_tokens", "model_context_window_exceeded")


def is_model_id(model: str) -> bool:
    # shared with config.py so both apply the same rule
    return bool(_MODEL_ID.match(model))


class LLMError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotEnoughPlacesError(LLMError):
    pass


class LLMUnavailableError(LLMError):
    pass


class LLMConfigError(LLMError):
    pass


class LLMRefusedError(LLMError):
    pass


class LLMTruncatedError(LLMError):
    pass


class LLMInvalidOutputError(LLMError):
    pass


class AgentUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    attempts: int = Field(ge=1, le=MAX_ATTEMPTS)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_s: float = Field(ge=0)
    request_ids: tuple[str, ...]


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    itinerary: Itinerary
    usage: AgentUsage


def _feedback(exc: ItineraryValidationError) -> str:
    # only our own bounded messages go back to the model, never its raw output
    reason = exc.message + (f": {exc.detail}" if exc.detail else "")
    return (
        f"\n\nA previous answer was rejected: {reason}. "
        "Follow the rules and the schema exactly."
    )


class LLMAgent:
    def __init__(
        self,
        client: AsyncAnthropic,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        *,
        sdk_max_retries: int = SDK_MAX_RETRIES,
        attempt_timeout_s: float = ATTEMPT_TIMEOUT_S,
        total_timeout_s: float = TOTAL_TIMEOUT_S,
        clock: Clock = time.monotonic,
    ) -> None:
        if not is_model_id(model):
            raise ValueError("model must be a plain model id")
        if effort not in ALLOWED_EFFORTS:
            raise ValueError(f"effort must be one of {', '.join(ALLOWED_EFFORTS)}")
        if sdk_max_retries < 0:
            raise ValueError("sdk_max_retries must not be negative")
        if attempt_timeout_s <= 0 or total_timeout_s <= 0:
            raise ValueError("timeouts must be positive")
        # our bounds win over whatever the shared client was built with
        self._client = client.with_options(timeout=attempt_timeout_s, max_retries=sdk_max_retries)
        self._model = model
        self._effort = effort
        self._total_timeout_s = total_timeout_s
        self._clock = clock

    async def plan(self, request: UserRequest, candidates: PlaceCandidates) -> AgentResult:
        # every day needs its own stop, so do not pay for a call that cannot succeed
        if len(candidates.places) < request.days:
            raise NotEnoughPlacesError("not enough places for the requested number of days")
        system = build_system_prompt()
        user = build_user_prompt(request, candidates)
        schema = build_output_schema(request, candidates)
        layout = slot_layout(request, candidates)
        start = self._clock()
        input_tokens = 0
        output_tokens = 0
        request_ids: list[str] = []
        feedback = ""
        try:
            async with asyncio.timeout(self._total_timeout_s):
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    message = await self._call(system, user + feedback, schema)
                    input_tokens += message.usage.input_tokens
                    output_tokens += message.usage.output_tokens
                    if message._request_id:
                        request_ids.append(message._request_id)
                    logger.info(
                        "llm attempt %d model %s stop %s tokens in %d out %d request %s",
                        attempt,
                        self._model,
                        message.stop_reason,
                        message.usage.input_tokens,
                        message.usage.output_tokens,
                        message._request_id,
                    )
                    text = _answer_text(message)
                    try:
                        itinerary = resolve(parse_slot_answer(text, layout), candidates, request)
                    except ItineraryValidationError as exc:
                        logger.warning("llm attempt %d rejected: %s", attempt, exc.message)
                        feedback = _feedback(exc)
                        continue
                    usage = AgentUsage(
                        model=self._model,
                        attempts=attempt,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        latency_s=round(self._clock() - start, 3),
                        request_ids=tuple(request_ids),
                    )
                    return AgentResult(itinerary=itinerary, usage=usage)
        except TimeoutError:
            logger.warning("llm timed out after %.0f s", self._total_timeout_s)
            raise LLMUnavailableError("model timed out") from None
        raise LLMInvalidOutputError("model output failed validation twice")

    async def _call(self, system: str, user: str, schema: dict[str, Any]) -> Any:
        try:
            return await self._client.messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        # from None everywhere: sdk errors carry response bodies, our messages stay safe
        except anthropic.APITimeoutError:
            logger.warning("llm request timed out")
            raise LLMUnavailableError("model request timed out") from None
        except anthropic.APIConnectionError:
            logger.warning("llm connection failed")
            raise LLMUnavailableError("could not reach the model api") from None
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            logger.error("llm auth failed, http %s request %s", exc.status_code, exc.request_id)
            raise LLMConfigError("model api rejected the credentials, check ANTHROPIC_API_KEY") from None
        except anthropic.BadRequestError as exc:
            # also covers a reached spend limit and a schema the api does not accept
            logger.error("llm bad request, request %s", exc.request_id)
            raise LLMConfigError("model api rejected the request") from None
        except anthropic.RateLimitError as exc:
            logger.warning("llm rate limited, request %s", exc.request_id)
            raise LLMUnavailableError("model api is rate limited, try again later") from None
        except anthropic.APIStatusError as exc:
            logger.warning("llm api error, http %s request %s", exc.status_code, exc.request_id)
            raise LLMUnavailableError(f"model api returned http {exc.status_code}") from None


def _answer_text(message: Any) -> str:
    reason = message.stop_reason
    if reason == "refusal":
        raise LLMRefusedError("model declined to plan this request")
    if reason in _TRUNCATED:
        raise LLMTruncatedError("model output was cut off")
    if reason != "end_turn":
        raise LLMInvalidOutputError(f"unexpected stop reason {reason}")
    # thinking blocks come first, only text blocks carry the json answer
    return "".join(block.text for block in message.content if block.type == "text")