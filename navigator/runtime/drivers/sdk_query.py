import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Callable

DEFAULT_SYSTEM_PROMPT = (
    "You are a Navigator subagent. Reply with ONLY the requested JSON object \u2014 "
    "no prose, no markdown fences.")

# operator decision 2026-10-15: the builder runs on claude-fable-5-1, whose credits run
# out first; an account/model error must move the SAME query to the next model in the
# chain instead of failing the run (2026-08-25..10-09: 216 cycles lost to a masked Fable
# credits error; 2026-10-15: a retry burned on 'session limit'). Only Settings / the
# driver pass this chain: make_sdk_query_fn's own default is () so a bare query fn
# behaves exactly as it always did.
DEFAULT_MODEL_FALLBACKS: tuple[str, ...] = ("claude-opus-5-5", "claude-sonnet-5-5")
DEFAULT_MODEL_COOLDOWN_S = 3600


def model_chain(model, fallbacks) -> list:
    """Models to try, in order, for a role configured on `model`.

    Never falls back UP: a model that is itself in the chain only tries the
    models after it (a Sonnet planner never calls Opus or Fable). Duplicates keep
    their first position; empty entries are dropped.
    """
    if not model or not fallbacks:
        return [model]
    ordered = [m for m in dict.fromkeys(fallbacks) if m]
    if model in ordered:
        return ordered[ordered.index(model):]
    return [model] + ordered


def make_sdk_query_fn(*, model: str | None = None,
                      system_prompt: str = DEFAULT_SYSTEM_PROMPT,
                      timeout_s: float = 300.0,
                      logger=None,
                      fallbacks: tuple[str, ...] = (),
                      cooldown_s: float = DEFAULT_MODEL_COOLDOWN_S,
                      cooldowns: dict | None = None,
                      clock: Callable[[], float] = time.monotonic) -> Callable[[str], str]:
    """Build a (prompt) -> str query_fn backed by claude-agent-sdk.

    Imported lazily so the package works without the sdk extra; the returned
    fn is what ClaudeSDKDriver consumes. Tools are disabled and turns capped
    at 1: subagent roles only emit JSON \u2014 Navigator's own runner executes.

    Each SDK query emits exactly one metrics event on `logger`
    ('sdk_query.completed' or 'sdk_query.failed'); logging failures never
    affect results. Pass a logger to observe metrics without touching global
    logging configuration.

    Model fallback (off unless `fallbacks` is non-empty AND `model` is set):
    a model-specific error (usage credits, credit balance, usage/weekly/Opus limit,
    unsupported, unknown or inaccessible model) re-issues the same prompt on the
    next model of model_chain(model, fallbacks); the failing model enters
    `cooldowns` (shared across query fns when the caller passes one dict) as
    (expiry, account_wide=False) for `cooldown_s` seconds of `clock` time and
    later calls skip it. One 'sdk_query.model_fallback' event per switch carries
    the marker only, never the error text. An account-wide error ('session
    limit') never falls back: every model of THIS fn's chain cools down at once
    (account_wide=True), one 'sdk_query.account_cooldown' event is emitted and
    the error is raised unchanged after a single SDK call. When every model of
    the chain is cooling, a call raises RuntimeError('claude-agent-sdk: all
    models cooling down until <iso>') without any SDK call. Timeouts, 'not
    logged in' and every other error are never fallen back; if the whole chain
    fails the last error is raised unchanged.
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

    fallbacks = tuple(fallbacks or ())
    chain = model_chain(model, fallbacks)
    fallback_enabled = bool(model) and bool(fallbacks)
    if cooldowns is None:
        cooldowns = {}

    def _emit(ok: bool, fields: dict) -> None:
        # metrics are best-effort: a broken logger must never change a result
        try:
            if ok:
                logger.info("sdk_query.completed", **fields)
            else:
                logger.warning("sdk_query.failed", **fields)
        except Exception:
            pass

    def _emit_fallback(from_model, to_model, reason: str) -> None:
        # marker only: the full error text already went to sdk_query.failed
        try:
            logger.warning("sdk_query.model_fallback", from_model=from_model,
                           to_model=to_model, reason=reason)
        except Exception:
            pass

    def _emit_account_cooldown(use_model, reason: str) -> None:
        # marker only, never the error text; `models` lists every model now cooling
        try:
            logger.warning("sdk_query.account_cooldown", model=use_model, reason=reason,
                           models=list(chain), cooldown_s=cooldown_s)
        except Exception:
            pass

    def _run_query(prompt: str, meta: dict, use_model) -> str:
        # max_turns=4 not 1: the CLI occasionally burns turns before the
        # final reply (observed live 2026-07-08, and again 2026-07-18 at 2).
        # Tools are disabled, so unused headroom costs nothing.
        # setting_sources=[] keeps the operator's hooks/plugins/skills out of
        # the subagent session \u2014 with them loaded, skill reminders push the
        # model into tool calls until max_turns errors (observed same day).
        options = ClaudeAgentOptions(system_prompt=system_prompt, model=use_model,
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

    def _query_once(prompt: str, use_model) -> str:
        meta: dict = {"num_turns": None, "subtype": None}
        start = time.monotonic()

        def _fields(reply_chars: int) -> dict:
            # `model` is the one actually queried, so fallbacks are visible per event
            return {"model": use_model, "elapsed_s": round(time.monotonic() - start, 3),
                    "num_turns": meta.get("num_turns"), "subtype": meta.get("subtype"),
                    "reply_chars": reply_chars}

        try:
            text = _run_query(prompt, meta, use_model)
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

    def _attempt(prompt: str, use_model) -> str:
        # one bounded retry for model-side flakes (max-turns error results,
        # missing result message, truncated/non-JSON output \u2014 all observed
        # live on fable-5 through 2026-07-18). Wall-clock timeouts are NOT
        # retried: doubling a 300s wait starves the tick cadence.
        # With fallback disabled this is byte-for-byte the historical behaviour.
        try:
            text = _query_once(prompt, use_model)
        except TimeoutError:
            raise
        except Exception as e:
            if fallback_enabled and _fallback_marker(e) is not None:
                raise  # the chain switches model; a same-model retry would only repeat it
            if not _is_retryable(e):
                raise
            return _query_once(prompt, use_model)
        if _is_valid_json_object(text):
            return text
        retry = _query_once(prompt, use_model)
        # still bad after the retry: hand it to the driver's strict parser so
        # the failure surfaces with head/tail detail \u2014 never invent structure
        return retry

    def _until(m) -> float:
        # entries are (expiry, account_wide); bare floats written by older callers
        # are read as model-specific expiries
        entry = cooldowns.get(m)
        if entry is None:
            return float("-inf")
        if isinstance(entry, (tuple, list)):
            return float(entry[0])
        return float(entry)

    def _cooling(m, now: float) -> bool:
        return _until(m) > now

    def query_fn(prompt: str) -> str:
        if not fallback_enabled:
            return _attempt(prompt, model)
        now = clock()
        # a model still cooling down from a recent account error is skipped; when every
        # model is cooling, fail fast without an SDK call instead of re-querying the
        # whole chain on every call (cold audit of N7 rev5)
        candidates = [m for m in chain if not _cooling(m, now)]
        if not candidates:
            earliest = min(_until(m) for m in chain)
            wall = time.time() + (earliest - now)
            until_iso = datetime.fromtimestamp(wall, tz=timezone.utc).isoformat(timespec="seconds")
            raise RuntimeError(f"claude-agent-sdk: all models cooling down until {until_iso}")
        for i, use_model in enumerate(candidates):
            try:
                return _attempt(prompt, use_model)
            except TimeoutError:
                raise  # never fallen back: a second full wait starves the tick
            except Exception as e:
                marker = _fallback_marker(e)
                if marker is None:
                    raise  # 'not logged in' and everything else: today's behaviour
                until = clock() + cooldown_s
                if marker in _ACCOUNT_WIDE_MARKERS:
                    # the whole account is limited: falling back would only burn more
                    # failing calls, so every model of the chain cools and the error
                    # surfaces unchanged after this single call
                    for m in chain:
                        cooldowns[m] = (until, True)
                    _emit_account_cooldown(use_model, marker)
                    raise
                cooldowns[use_model] = (until, False)
                if i == len(candidates) - 1:
                    raise  # chain exhausted: the last error surfaces unchanged
                _emit_fallback(use_model, candidates[i + 1], marker)
        raise AssertionError("unreachable: model chain exhausted without an error")

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


# account/model failures that justify the next model in the chain. These do NOT
# change _is_retryable: with fallback disabled the retry logic is untouched.
# Only the marker ever reaches the fallback log event, never the error text.
# 'session limit' is account-wide: every model fails the same way, so the chain cools as
# a whole and no fallback call is made. Everything else is model-specific: credits are
# per model (Fable needs its own), weekly/Opus limits name a model, and 'issue with the
# selected model' is Claude Code's invalid/no-access model message.
_ACCOUNT_WIDE_MARKERS = ("session limit",)
_FALLBACK_MARKERS = ("session limit", "usage credits", "credit balance",
                     "does not support this model", "issue with the selected model",
                     "usage limit", "weekly limit", "opus limit", "limit for this model",
                     "model not available", "model not found")


def _fallback_marker(e: Exception) -> str | None:
    msg = str(e).lower()
    for marker in _FALLBACK_MARKERS:
        if marker in msg:
            return marker
    # the API's not_found_error only means "no such model" when it says so:
    # session_not_found / file_not_found must never switch models
    if "not_found_error" in msg and "model" in msg:
        return "not_found_error"
    return None


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
