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
                                     max_turns=4, tools=[], allowed_tools=[],
                                     setting_sources=[], settings=_LOCKED_SETTINGS)
        results: list[str] = []
        error: list[str] = []

        async def _collect() -> None:
            # the SDK's own exception for an is_error result only says "error result:
            # success"; record the real reason and let the stream finish (raising
            # mid-iteration leaves the generator half-closed)
            try:
                async for message in query(prompt=prompt, options=options):
                    if not isinstance(message, ResultMessage):
                        continue
                    if getattr(message, "is_error", False):
                        error.append(_describe_error(message))
                    elif message.result is not None:
                        results.append(message.result)
            except Exception:
                if not error:
                    raise

        # wall-clock guard: the CLI subprocess can stall during init under
        # launchd (observed 2026-07-17 — daemon froze on cycle 1 for 19h);
        # a stall must raise so the daemon loop retries next tick
        try:
            asyncio.run(asyncio.wait_for(_collect(), timeout=timeout_s))
        except (asyncio.TimeoutError, TimeoutError) as e:
            raise TimeoutError(f"claude-agent-sdk query timed out after {timeout_s}s") from e
        if error:
            raise RuntimeError(f"claude-agent-sdk error result: {error[-1]}")
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


# subagents only return JSON: tools=[] drops the built-in toolset (allowed_tools=[]
# alone leaves it visible), and this drops the account's claude.ai connectors
_LOCKED_SETTINGS = json.dumps({"disableClaudeAiConnectors": True})


def _describe_error(message) -> str:
    if message.result:
        return str(message.result)
    errors = getattr(message, "errors", None) or []
    return f"{getattr(message, 'subtype', 'unknown')}: {'; '.join(map(str, errors)) or 'no detail'}"


# account/config failures: a retry just doubles the cost of a guaranteed error
_NON_RETRYABLE = ("usage credits", "does not support this model", "session limit",
                  "usage limit", "not logged in")

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
