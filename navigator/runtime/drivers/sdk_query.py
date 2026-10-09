import asyncio
import json
import re
from typing import Callable

DEFAULT_SYSTEM_PROMPT = (
    "You are a Navigator subagent. Reply with ONLY the requested JSON object — "
    "no prose, no markdown fences.")

def make_sdk_query_fn(*, model: str | None = None,
                      system_prompt: str = DEFAULT_SYSTEM_PROMPT,
                      timeout_s: float = 300.0) -> Callable[[str], str]:
    """Build a (prompt) -> str query_fn backed by claude-agent-sdk.

    Imported lazily so the package works without the sdk extra; the returned
    fn is what ClaudeSDKDriver consumes. Tools are disabled and turns capped
    at 1: subagent roles only emit JSON — Navigator's own runner executes.
    """
    try:
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
    except ImportError as e:
        raise ValueError(
            "driver 'claude_sdk' needs the sdk extra: uv sync --extra sdk "
            "(claude-agent-sdk not installed)") from e

    def _query_once(prompt: str) -> str:
        # max_turns=4 not 1: the CLI occasionally burns turns before the
        # final reply (observed live 2026-07-08, and again 2026-07-18 at 2).
        # Tools are disabled, so unused headroom costs nothing.
        # setting_sources=[] keeps the operator's hooks/plugins/skills out of
        # the subagent session — with them loaded, skill reminders push the
        # model into tool calls until max_turns errors (observed same day).
        options = ClaudeAgentOptions(system_prompt=system_prompt, model=model,
                                     max_turns=4, allowed_tools=[],
                                     setting_sources=[])
        results: list[str] = []

        async def _collect() -> None:
            async for message in query(prompt=prompt, options=options):
                # the SDK's own exception for an is_error result only says
                # "error result: success"; the real reason is in .result
                if isinstance(message, ResultMessage) and getattr(message, "is_error", False):
                    raise RuntimeError(f"claude-agent-sdk error result: {message.result}")
                if isinstance(message, ResultMessage) and message.result is not None:
                    results.append(message.result)

        # wall-clock guard: the CLI subprocess can stall during init under
        # launchd (observed 2026-07-17 — daemon froze on cycle 1 for 19h);
        # a stall must raise so the daemon loop retries next tick
        try:
            asyncio.run(asyncio.wait_for(_collect(), timeout=timeout_s))
        except (asyncio.TimeoutError, TimeoutError) as e:
            raise TimeoutError(f"claude-agent-sdk query timed out after {timeout_s}s") from e
        if not results:
            raise RuntimeError("claude-agent-sdk returned no result message")
        return _strip_fences(results[-1])

    def query_fn(prompt: str) -> str:
        # one bounded retry for model-side flakes (max-turns error results,
        # missing result message, truncated/non-JSON output — all observed
        # live on fable-5 through 2026-07-18). Wall-clock timeouts are NOT
        # retried: doubling a 300s wait starves the tick cadence.
        try:
            text = _query_once(prompt)
        except TimeoutError:
            raise
        except Exception as e:
            if not _is_retryable(e):
                raise
            return _query_once(prompt)
        if _is_valid_json_object(text):
            return text
        retry = _query_once(prompt)
        # still bad after the retry: hand it to the driver's strict parser so
        # the failure surfaces with head/tail detail — never invent structure
        return retry

    return query_fn


# account/config failures: a retry just doubles the cost of a guaranteed error
_NON_RETRYABLE = ("usage credits", "does not support this model")

def _is_retryable(e: Exception) -> bool:
    msg = str(e).lower()
    if any(marker in msg for marker in _NON_RETRYABLE):
        return False
    if isinstance(e, RuntimeError) and "no result message" in msg:
        return True
    return "maximum number of turns" in msg or "error result" in msg


def _is_valid_json_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


def _strip_fences(text: str) -> str:
    # live models fence JSON and wrap it in prose despite instructions
    # (observed 2026-07-15: fable-5 planner reply); the driver parser stays
    # strict, so bare JSON is this adapter's responsibility
    stripped = text.strip()
    match = re.search(r"```(?:\w+)?\n(.*?)```", stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    if not stripped.startswith("{"):
        start, end = stripped.find("{"), stripped.rfind("}")
        if start != -1 and end > start:
            return stripped[start:end + 1]
    return stripped
