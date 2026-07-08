import asyncio
from typing import Callable

DEFAULT_SYSTEM_PROMPT = (
    "You are a Navigator subagent. Reply with ONLY the requested JSON object — "
    "no prose, no markdown fences.")

def make_sdk_query_fn(*, model: str | None = None,
                      system_prompt: str = DEFAULT_SYSTEM_PROMPT) -> Callable[[str], str]:
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

    def query_fn(prompt: str) -> str:
        # max_turns=2 not 1: the CLI occasionally burns a turn before the
        # final reply and errors out at 1 (observed live 2026-07-08).
        # setting_sources=[] keeps the operator's hooks/plugins/skills out of
        # the subagent session — with them loaded, skill reminders push the
        # model into tool calls until max_turns errors (observed same day).
        options = ClaudeAgentOptions(system_prompt=system_prompt, model=model,
                                     max_turns=2, allowed_tools=[],
                                     setting_sources=[])
        results: list[str] = []

        async def _collect() -> None:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage) and message.result is not None:
                    results.append(message.result)

        asyncio.run(_collect())
        if not results:
            raise RuntimeError("claude-agent-sdk returned no result message")
        return _strip_fences(results[-1])

    return query_fn


def _strip_fences(text: str) -> str:
    # live models fence JSON despite instructions; the driver parser stays
    # strict, so bare JSON is this adapter's responsibility
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            return stripped[first_newline + 1:-3].strip()
    return stripped
