import asyncio
import json
import re
import time
from typing import Callable

DEFAULT_SYSTEM_PROMPT = (
    "You are a Navigator subagent. Reply with ONLY the requested JSON object \u2014 "
    "no prose, no markdown fences.")

def make_sdk_query_fn(*, model: str | None = None,
                      system_prompt: str = DEFAULT_SYSTEM_PROMPT,
                      timeout_s: float = 300.0,
                      logger=None) -> Callable[[str], str]:
    """Build a (prompt) -> str query_fn backed by claude-agent-sdk.

    Imported lazily so the package works without the sdk extra; the returned
    fn is what ClaudeSDKDriver consumes. Tools are disabled and turns capped
    at 1: subagent roles only emit JSON \u2014 Navigator's own runner executes.

    Each SDK query emits exactly one metrics event on `logger`
    ('sdk_query.completed' or 'sdk_query.failed'); logging failures never
    affect results. Pass a logger to observe metrics without touching global
    logging configuration.
    """
    try:
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
    except ImportError as e:
        raise ValueError(
            "driver 'claude_sdk' needs the sdk extra: uv sync --extra sdk "
            "(claude-agent-sdk not installed)") from e

    if logger is None:
        from navigator.observability.logging import get_logger
        logger = get_logger("navigator.sdk")

    def _emit(ok: bool, fields: dict) -> None:
        # metrics are best-effort: a broken logger must never change a result
        try:
            if ok:
                logger.info("sdk_query.completed", **fields)
            else:
                logger.warning("sdk_query.failed", **fields)
        except Exception:
            pass

    def _run_query(prompt: str, meta: dict) -> str:
        # max_turns=4 not 1: the CLI occasionally burns turns before the
        # final reply (observed live 2026-07-08, and again 2026-07-18 at 2).
        # Tools are disabled, so unused headroom costs nothing.
        # setting_sources=[] keeps the operator's hooks/plugins/skills out of
        # the subagent session \u2014 with them loaded, skill reminders push the
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
                    try:
                        meta["num_turns"] = getattr(message, "num_turns", None)
                        meta["subtype"] = getattr(message, "subtype", None)
                    except Exception:
                        pass
                    if getattr(message, "is_error", False):
                        error.append(_describe_error(message))
                    elif message.result is not None:
                        results.append(message.result)
            except Exception:
                if not error:
                    raise

        # wall-clock guard: the CLI subprocess can stall during init under
        # launchd (observed 2026-07-17 \u2014 daemon froze on cycle 1 for 19h);
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

    def _query_once(prompt: str) -> str:
        meta: dict = {"num_turns": None, "subtype": None}
        start = time.monotonic()

        def _fields(reply_chars: int) -> dict:
            return {"model": model, "elapsed_s": round(time.monotonic() - start, 3),
                    "num_turns": meta.get("num_turns"), "subtype": meta.get("subtype"),
                    "reply_chars": reply_chars}

        try:
            text = _run_query(prompt, meta)
        except TimeoutError as e:
            fields = _fields(0)
            fields["timeout_s"] = timeout_s
            fields["error"] = str(e)[:300]
            _emit(False, fields)
            raise
        except Exception as e:
            fields = _fields(0)
            fields["error"] = str(e)[:300]
            _emit(False, fields)
            raise
        _emit(True, _fields(len(text)))
        return text

    def query_fn(prompt: str) -> str:
        # one bounded retry for model-side flakes (max-turns error results,
        # missing result message, truncated/non-JSON output \u2014 all observed
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
        # the failure surfaces with head/tail detail \u2014 never invent structure
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
    start = stripped.find("{")
    if start == -1:
        return stripped
    try:
        # first complete JSON object; drops prose before AND after it
        _, end = json.JSONDecoder().raw_decode(stripped, start)
        return stripped[start:end]
    except ValueError:
        if start > 0:
            last = stripped.rfind("}")
            if last > start:
                return stripped[start:last + 1]
        return stripped[start:]
