import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, NoReturn

from navigator.safety.errors import QuotaExhaustedError

DEFAULT_SYSTEM_PROMPT = (
    "You are a Navigator subagent. Reply with ONLY the requested JSON object \u2014 "
    "no prose, no markdown fences.")

TOOLED_SYSTEM_PROMPT_SUFFIX = (
    " You may use Read, Grep and Glob to inspect files inside the worktree (your current "
    "directory) before answering. The operator's files outside it, Write, Edit, Bash and the "
    "network are denied and only waste turns. Your final reply must still be ONLY the requested "
    "JSON object.")

# build 2b: appended to the tooled prompt ONLY when the call passes mcp_servers (the builder's
# run_tests server); the model reads this next to builder_tools.TOOL_DESCRIPTION
TESTS_SYSTEM_PROMPT_SUFFIX = (
    " You also have run_tests(paths=[...]): it runs the repository's allowlisted pytest command (plus -q, plus "
    "the given paths relative to the run directory) inside your worktree and returns rc= and the output tail. "
    "It is bounded per build; a call that is rejected still counts. IMPORTANT: your JSON file_edits/file_writes "
    "are applied AFTER you reply, so run_tests exercises the files as they are NOW; use it to understand the "
    "current state and to check existing tests, not to validate edits you have not made yet.")

# operator decision 2026-10-15: the builder runs on claude-fable-5-1, whose credits run
# out first; an account/model error must move the SAME query to the next model in the
# chain instead of failing the run (2026-08-25..10-09: 216 cycles lost to a masked Fable
# credits error; 2026-10-15: a retry burned on 'session limit'). Only Settings / the
# driver pass this chain: make_sdk_query_fn's own default is () so a bare query fn
# behaves exactly as it always did.
DEFAULT_MODEL_FALLBACKS: tuple[str, ...] = ("claude-opus-5-5", "claude-sonnet-5-5")
DEFAULT_MODEL_COOLDOWN_S = 3600
# 'issue with the selected model' says "try again": a short cooldown, not an hour
SELECTED_MODEL_COOLDOWN_S = 300
# when every model is cooling, one probe call per interval checks whether the limit lifted
ALL_COOLING_PROBE_INTERVAL_S = 300
# a reset time parsed from the error text ('resets 4:20am') is clamped to this window
MIN_RESET_COOLDOWN_S = 60
MAX_RESET_COOLDOWN_S = 6 * 3600
# key in the shared cooldowns dict holding the clock time of the last all-cooling probe
_PROBE_KEY = "__all_cooling_probe_at__"
# key in the shared cooldowns dict holding the account-wide cooldown expiry (N7e): a
# 'session limit' blocks every model of every role, so it is stored ONCE, apart from the
# model-specific entries that must survive it (Fable out of credits stays out of the
# chain after the account limit lifts)
_ACCOUNT_KEY = "__account_cooldown_until__"


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
                      clock: Callable[[], float] = time.monotonic,
                      wall_clock: Callable[[], float] = time.time,
                      tools: tuple[str, ...] = (),
                      max_turns: int = 4) -> Callable[[str], str]:  # query_fn(prompt, *, cwd=None)
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
    later calls skip it. When the error text names a reset time ('resets
    4:20am', optionally followed by '(America/New_York)'; default local time,
    read from `wall_clock`) the cooldown ends at the next such wall time
    instead, clamped to [60s, 6h]; 'issue with the selected model' ("try
    again") cools its model for 5 minutes. One 'sdk_query.model_fallback' event
    per switch carries the marker only, never the error text. An account-wide
    error ('session limit') never falls back: its expiry is stored once under the
    reserved _ACCOUNT_KEY (model-specific entries are left as they are, so a
    model out of credits stays out of the chain after the account limit lifts),
    one 'sdk_query.account_cooldown' event is emitted and the error is raised
    unchanged after a single SDK call; a live account-wide expiry (or a legacy
    (expiry, True) model entry written by an older navigator) then blocks EVERY
    model of EVERY fn sharing the dict. When every model of the chain is cooling, a call raises
    RuntimeError('claude-agent-sdk: all models cooling down until <iso>')
    without any SDK call, except that at most once per 5 minutes (per shared
    dict) a single probe call checks whether the limit lifted. The probe goes to
    the first model of the chain that is blocked ONLY by an account-wide entry
    (a model still in its own cooldown would fail for its own reason and say
    nothing about the account); when every model of the chain is in its own
    cooldown no probe is made. On success every account-wide entry is cleared,
    the probe gate is reset and the reply returned; on a limit error the
    cooldowns are refreshed (a live model-specific cooldown is never extended)
    and the all-cooling error is raised. A successful probe resets the probe
    gate, and so does any call that finds candidates (no account-wide block is
    live, so the outage is over): the next outage again starts with a zero-call
    failure and probes only after the interval. Timeouts, 'not logged in' and
    every other error are never fallen back; if the whole chain fails the last
    error is raised unchanged. An account-wide error (and an all-cooling failure
    caused by one) is raised as navigator.safety.errors.QuotaExhaustedError, a
    RuntimeError subclass with the same message plus .marker and .resets_at (ISO-8601
    UTC, clamped like the cooldown, or None).

    Builder tools (off unless `tools` is non-empty AND the call passes cwd): the query
    runs with those tools allowed, cwd set to the worktree, max_turns=`max_turns` and
    _TOOLED_SETTINGS; with `tools` empty, or cwd None, the options are exactly the
    tool-less ones (max_turns 4, tools=[], _LOCKED_SETTINGS, no cwd). A call may also
    pass mcp_servers (ClaudeAgentOptions.mcp_servers, e.g. the builder's run_tests
    server) and mcp_tools (their qualified names, appended to allowed_tools); both take
    effect in that tooled branch only and are ignored by the tool-less one.
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
    tools = tuple(tools or ())
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

    def _emit_account_cooldown(use_model, reason: str, delay_s: float) -> None:
        # marker only, never the error text; `models` lists every model now cooling
        try:
            logger.warning("sdk_query.account_cooldown", model=use_model, reason=reason,
                           models=list(chain), cooldown_s=delay_s)
        except Exception:
            pass

    def _run_query(prompt: str, meta: dict, use_model, *, cwd: str | None = None,
                   mcp_servers: dict | None = None, mcp_tools: tuple[str, ...] = ()) -> str:
        # max_turns=4 not 1: the CLI occasionally burns turns before the
        # final reply (observed live 2026-07-08, and again 2026-07-18 at 2).
        # Tools are disabled, so unused headroom costs nothing.
        # setting_sources=[] keeps the operator's hooks/plugins/skills out of
        # the subagent session \u2014 with them loaded, skill reminders push the
        # model into tool calls until max_turns errors (observed same day).
        if tools and cwd:
            # tooled builder: read-only tools inside the worktree, more turns (reads cost turns),
            # deny list from _TOOLED_SETTINGS; the reply contract is unchanged. Build 2: in-process
            # MCP servers (mcp_servers) and their qualified tool names (mcp_tools) join this
            # session only; `tools` stays the built-in set
            allowed = list(tools) + [t for t in mcp_tools if t not in tools]
            extra = {"mcp_servers": mcp_servers} if mcp_servers else {}
            options = ClaudeAgentOptions(system_prompt=system_prompt + TOOLED_SYSTEM_PROMPT_SUFFIX
                                         + (TESTS_SYSTEM_PROMPT_SUFFIX if mcp_servers else ""),
                                         model=use_model, max_turns=max_turns, tools=list(tools),
                                         allowed_tools=allowed, cwd=cwd, setting_sources=[],
                                         settings=_TOOLED_SETTINGS, **extra)
        else:
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

    def _query_once(prompt: str, use_model, *, cwd: str | None = None,
                    mcp_servers: dict | None = None, mcp_tools: tuple[str, ...] = ()) -> str:
        meta: dict = {"num_turns": None, "subtype": None}
        start = time.monotonic()

        def _fields(reply_chars: int) -> dict:
            # `model` is the one actually queried, so fallbacks are visible per event
            return {"model": use_model, "elapsed_s": round(time.monotonic() - start, 3),
                    "num_turns": meta.get("num_turns"), "subtype": meta.get("subtype"),
                    "reply_chars": reply_chars}

        try:
            text = _run_query(prompt, meta, use_model, cwd=cwd, mcp_servers=mcp_servers,
                              mcp_tools=mcp_tools)
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

    def _classify(e: Exception) -> Exception:
        # N16: an account-wide error becomes QuotaExhaustedError (same message, plus the
        # marker and a bounded resets_at) so consumers can defer instead of failing;
        # anything else is returned as it is
        if isinstance(e, QuotaExhaustedError):
            return e
        marker = _account_marker(e)
        if marker is None:
            return e
        wall_now = wall_clock()
        delay = _reset_delay_s(str(e), wall_now)
        if delay is None and fallback_enabled:
            delay = float(cooldown_s)  # the cooldown the chain actually enters
        return QuotaExhaustedError(str(e), marker=marker,
                                   resets_at=_resets_at_iso(wall_now, delay))

    def _raise_classified(e: Exception) -> NoReturn:
        classified = _classify(e)
        if classified is not e:
            raise classified from e
        raise e

    def _attempt(prompt: str, use_model, *, cwd: str | None = None,
                 mcp_servers: dict | None = None, mcp_tools: tuple[str, ...] = ()) -> str:
        # one bounded retry for model-side flakes (max-turns error results,
        # missing result message, truncated/non-JSON output \u2014 all observed
        # live on fable-5 through 2026-07-18). Wall-clock timeouts are NOT
        # retried: doubling a 300s wait starves the tick cadence.
        # With fallback disabled this is byte-for-byte the historical behaviour.
        try:
            text = _query_once(prompt, use_model, cwd=cwd, mcp_servers=mcp_servers,
                               mcp_tools=mcp_tools)
        except TimeoutError:
            raise
        except Exception as e:
            if fallback_enabled and _fallback_marker(e) is not None:
                # the chain switches model; a same-model retry would only repeat it
                _raise_classified(e)
            if not _is_retryable(e):
                _raise_classified(e)
            try:
                return _query_once(prompt, use_model, cwd=cwd, mcp_servers=mcp_servers,
                                   mcp_tools=mcp_tools)
            except Exception as e2:
                _raise_classified(e2)
        if _is_valid_json_object(text):
            return text
        retry = _query_once(prompt, use_model, cwd=cwd, mcp_servers=mcp_servers,
                            mcp_tools=mcp_tools)
        # still bad after the retry: hand it to the driver's strict parser so
        # the failure surfaces with head/tail detail \u2014 never invent structure
        return retry

    def _model_until(m) -> float:
        # the model's OWN cooldown only: a bare float or an (expiry, account_wide=False)
        # entry; account-wide entries are read by _account_until instead
        entry = cooldowns.get(m)
        if entry is None:
            return float("-inf")
        if isinstance(entry, (tuple, list)):
            if len(entry) > 1 and entry[1]:
                return float("-inf")
            return float(entry[0])
        return float(entry)

    def _account_until(now: float) -> float:
        # the live account-wide expiry of the SHARED dict: a session limit hit by any role
        # blocks every model of every role. N7e stores it once under _ACCOUNT_KEY; legacy
        # (expiry, True) model entries written by an older navigator still count
        best = float("-inf")
        for key, entry in cooldowns.items():
            if key == _PROBE_KEY:
                continue
            if key == _ACCOUNT_KEY:
                expiry = float(entry[0]) if isinstance(entry, (tuple, list)) else float(entry)
            elif isinstance(entry, (tuple, list)) and len(entry) > 1 and entry[1]:
                expiry = float(entry[0])
            else:
                continue
            if expiry > now:
                best = max(best, expiry)
        return best

    def _blocked_until(m, account_until: float) -> float:
        return max(_model_until(m), account_until)

    def _clear_account_block() -> None:
        # the limit lifted: the account-wide expiry (any role's) and every legacy
        # (expiry, True) entry go; model-specific cooldowns stay exactly as they were
        for key in [k for k, v in cooldowns.items()
                    if k == _ACCOUNT_KEY
                    or (isinstance(v, (tuple, list)) and len(v) > 1 and v[1])]:
            del cooldowns[key]

    def _all_cooling_error(now: float) -> RuntimeError:
        account_until = _account_until(now)
        earliest = min(_blocked_until(m, account_until) for m in chain)
        wall = wall_clock() + (earliest - now)
        until_iso = datetime.fromtimestamp(wall, tz=timezone.utc).isoformat(timespec="seconds")
        message = f"claude-agent-sdk: all models cooling down until {until_iso}"
        if account_until > now:
            # N16: a live account-wide expiry blocks the chain: that is a quota, not a
            # set of per-model cooldowns
            return QuotaExhaustedError(message, marker="all models cooling", resets_at=until_iso)
        return RuntimeError(message)

    def _delay_s(e: Exception, marker: str) -> float:
        if marker == "issue with the selected model":
            return float(SELECTED_MODEL_COOLDOWN_S)
        parsed = _reset_delay_s(str(e), wall_clock())
        return float(cooldown_s) if parsed is None else parsed

    def _record_failure(use_model, e: Exception, marker: str) -> None:
        delay = _delay_s(e, marker)
        now = clock()
        until = now + delay
        if marker in _ACCOUNT_WIDE_MARKERS:
            # N7e F1: one account-wide expiry for the whole shared dict; the chain models'
            # own entries are left alone, so a model out of credits is neither probed nor
            # a candidate again when the account limit lapses
            cooldowns[_ACCOUNT_KEY] = until
            _emit_account_cooldown(use_model, marker, delay)
        elif _model_until(use_model) <= now:
            # N7d: a model-specific failure never extends a cooldown the model is still
            # serving (only an expired one is replaced); candidates and probe targets
            # never have a live one, so this is a guard, not a path
            cooldowns[use_model] = (until, False)

    def _probe_or_fail(prompt: str, now: float, *, cwd: str | None = None,
                       mcp_servers: dict | None = None, mcp_tools: tuple[str, ...] = ()) -> str:
        # every model is cooling: at most one probe call per interval (per shared dict);
        # between probes fail fast with zero SDK calls. The gate is reset by a successful
        # probe, or by the first call that finds candidates once the block lapsed (N7e F2).
        last = cooldowns.get(_PROBE_KEY)
        if last is None or now - float(last) < ALL_COOLING_PROBE_INTERVAL_S:
            if last is None:
                cooldowns[_PROBE_KEY] = now
            raise _all_cooling_error(now)
        # N7d S1: probe the first model blocked ONLY by the account-wide entry; a model
        # still in its own cooldown (Fable out of credits) would fail for its own reason
        # and say nothing about the account limit. Nothing qualifies: no probe at all.
        probe_model = next((m for m in chain if _model_until(m) <= now), None)
        if probe_model is None:
            raise _all_cooling_error(now)
        cooldowns[_PROBE_KEY] = now
        try:
            text = _attempt(prompt, probe_model, cwd=cwd, mcp_servers=mcp_servers,
                            mcp_tools=mcp_tools)
        except TimeoutError:
            raise
        except Exception as e:
            marker = _fallback_marker(e)
            if marker is None:
                raise
            _record_failure(probe_model, e, marker)
            raise _all_cooling_error(clock()) from e
        _clear_account_block()
        cooldowns.pop(probe_model, None)
        cooldowns.pop(_PROBE_KEY, None)
        return text

    def query_fn(prompt: str, *, cwd: str | None = None, mcp_servers: dict | None = None,
                 mcp_tools: tuple[str, ...] = ()) -> str:
        if not fallback_enabled:
            return _attempt(prompt, model, cwd=cwd, mcp_servers=mcp_servers, mcp_tools=mcp_tools)
        now = clock()
        # a model still cooling down from a recent account error is skipped; any live
        # account-wide entry in the shared dict blocks every model; when every model is
        # cooling, probe once per interval instead of re-querying the whole chain
        account_until = _account_until(now)
        candidates = [m for m in chain if _blocked_until(m, account_until) <= now]
        if not candidates:
            return _probe_or_fail(prompt, now, cwd=cwd, mcp_servers=mcp_servers,
                                  mcp_tools=mcp_tools)
        # N7e F2: candidates exist, so no account-wide block is live (one blocks every
        # model of every chain) and the outage the gate belonged to is over. Dropping it
        # here makes the next outage start with a zero-call failure and probe only after
        # the interval; a stale timestamp made its first all-cooling call probe at once.
        cooldowns.pop(_PROBE_KEY, None)
        for i, use_model in enumerate(candidates):
            try:
                return _attempt(prompt, use_model, cwd=cwd, mcp_servers=mcp_servers,
                                mcp_tools=mcp_tools)
            except TimeoutError:
                raise  # never fallen back: a second full wait starves the tick
            except Exception as e:
                marker = _fallback_marker(e)
                if marker is None:
                    raise  # 'not logged in' and everything else: today's behaviour
                # account-wide: the whole account is limited, so every model of the chain
                # cools and the error surfaces unchanged after this single call (falling
                # back would only burn more failing calls); otherwise only this model cools
                _record_failure(use_model, e, marker)
                if marker in _ACCOUNT_WIDE_MARKERS:
                    _raise_classified(e)
                if i == len(candidates) - 1:
                    raise  # chain exhausted: the last error surfaces unchanged
                _emit_fallback(use_model, candidates[i + 1], marker)
        raise AssertionError("unreachable: model chain exhausted without an error")

    return query_fn


# subagents only return JSON: tools=[] drops the built-in toolset (allowed_tools=[]
# alone leaves it visible), and this drops the account's claude.ai connectors
_LOCKED_SETTINGS = json.dumps({"disableClaudeAiConnectors": True})

# tooled builder (NAVIGATOR_BUILDER_TOOLS): read-only tools inside the worktree. Worktrees live
# under /tmp, so denying /Users/** (and its macOS firmlink twin /System/Volumes/Data/**) blocks
# the operator's home (calwbot, .env, ~/.claude, the Telegram inbox); other Claude sessions'
# scratchpads (/private/tmp/claude-*, /tmp/claude-*) and the per-user temp tree
# (/private/var/folders) are denied too; writes, shell and network tools are denied outright.
# Proven live 2026-10-28 (SDK 0.2.165): a file under /tmp read OK, the firmlink path, a
# scratchpad file and Write denied.
_TOOLED_SETTINGS = json.dumps({
    "disableClaudeAiConnectors": True,
    "permissions": {"deny": [
        "Read(//Users/**)", "Read(//System/Volumes/Data/**)", "Read(//private/var/folders/**)",
        "Read(//private/tmp/claude-*/**)", "Read(//tmp/claude-*/**)",
        "Read(**/.env*)", "Read(**/*.pem)", "Read(**/*.key)",
        "Grep(//Users/**)", "Grep(//System/Volumes/Data/**)", "Grep(//private/var/folders/**)",
        "Grep(//private/tmp/claude-*/**)", "Grep(//tmp/claude-*/**)",
        "Glob(//Users/**)", "Glob(//System/Volumes/Data/**)", "Glob(//private/var/folders/**)",
        "Glob(//private/tmp/claude-*/**)", "Glob(//tmp/claude-*/**)",
        "Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"]},
})


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


def _account_marker(e: Exception) -> str | None:
    """The first _ACCOUNT_WIDE_MARKERS entry named in the error text, else None."""
    msg = str(e).lower()
    for marker in _ACCOUNT_WIDE_MARKERS:
        if marker in msg:
            return marker
    return None


def _resets_at_iso(wall_now: float, delay_s: float | None) -> str | None:
    """ISO-8601 UTC wall time (seconds) `delay_s` after `wall_now`; None when unknown."""
    if delay_s is None:
        return None
    return datetime.fromtimestamp(wall_now + delay_s, tz=timezone.utc).isoformat(timespec="seconds")


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


# 'resets 4:20am', 'resets 10:30pm (America/New_York)': the limit's own reset time
_RESET_RE = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?"
    r"(?:\s*\(([A-Za-z][\w+\-]*(?:/[\w+\-]+)*)\))?", re.IGNORECASE)


def _reset_delay_s(text: str, wall_now: float) -> float | None:
    """Seconds from `wall_now` until the next '<H>[:MM]am|pm' named after 'resets' in
    `text`, read in the '(<IANA tz>)' that follows it (default: local time), clamped to
    [MIN_RESET_COOLDOWN_S, MAX_RESET_COOLDOWN_S]. None when the text names no reset time
    or it cannot be read; the caller then uses its fixed cooldown."""
    m = _RESET_RE.search(text)
    if m is None:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if m.group(3).lower() == "p" else 0)
    tz = None
    if m.group(4):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(m.group(4))
        except Exception:
            tz = None  # unknown zone name: fall back to local time
    try:
        now_dt = (datetime.fromtimestamp(wall_now, tz=tz) if tz is not None
                  else datetime.fromtimestamp(wall_now).astimezone())
        target = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now_dt:
            target += timedelta(days=1)
        delay = target.timestamp() - wall_now
    except (ValueError, OverflowError, OSError):
        return None
    return float(min(max(delay, MIN_RESET_COOLDOWN_S), MAX_RESET_COOLDOWN_S))
