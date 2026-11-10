"""Drift test (follow-up to e6932c6, merge_audit_2026-11-07): the orchestrator MCP server's registered
tool names versus the conductor's MAIN_TOOLS / ORC_WRITE_TOOLS.

Why: `--tools` does not govern MCP tool names. The main turn and every normal helper load the
orchestrator server, so a write tool is kept out ONLY by being named in ORC_WRITE_TOOLS
(disallowed_tools). A new write tool added to mcp_server.py that nobody adds to ORC_WRITE_TOOLS would be
reachable, unblocked, by the main turn and by helpers. These tests make that an immediate red.

The server file is read as TEXT (never imported: the orchestrator package pulls in its stores, mcp,
pydantic models). Three independent "write" signals are taken from the file and unioned, so a write
tool slips only if it is missing from the header, has a read-looking name AND calls no mutating store
method — and even then the "every registered name is accounted for" test catches it as unknown.
"""
import re
from pathlib import Path

import pytest

from navigator.conductor import runner

MCP_SERVER = Path("/Users/Manoj/calwbot/orchestrator/backend/mcp_server.py")
PREFIX = "mcp__orchestrator__"

# Registered read tools the main turn deliberately does not get (reachable, but not in MAIN_TOOLS).
NOT_FOR_MAIN_TURN = {"preview_prompt", "list_linkable_objects"}

# Registered tools known to be missing from the header docstring's READ/WRITE lists today. Anything else
# unlisted is a drift: the header is the human-maintained classification, so a tool absent from it is
# exactly how a write tool would escape the header signal.
KNOWN_HEADER_GAPS = {"search"}

# Store methods that only read. Any other `_<x>_store.<method>(` call inside a tool body is a mutation.
READ_STORE_METHODS = {"load", "list", "list_ids", "list_sessions", "detail_view", "by_category"}

# Function-name verbs that mean "write" in this server's vocabulary.
WRITE_VERBS = ("create_", "add_", "apply_", "save_", "update_", "convert_", "finalize_", "delete_",
               "remove_", "set_", "write_", "append_", "patch_", "archive_", "reset_", "clear_")

_TOOL_RE = re.compile(r"^@mcp\.tool\((?P<args>[^)]*)\)\s*\n(?:async\s+)?def\s+(?P<fn>\w+)\(", re.M)
_HEADER_LINE_RE = re.compile(r"^\s+(\w+)\s+—", re.M)


def _source() -> str:
    if not MCP_SERVER.is_file():
        pytest.skip(f"orchestrator MCP server not present at {MCP_SERVER}")
    return MCP_SERVER.read_text()


def registered_tools(src: str) -> dict[str, str]:
    """{tool name: function body} for every `@mcp.tool()`-decorated function. The MCP name is the
    decorator's name= argument when given, else the function name."""
    out: dict[str, str] = {}
    matches = list(_TOOL_RE.finditer(src))
    for i, m in enumerate(matches):
        name = m.group("fn")
        renamed = re.search(r"name\s*=\s*['\"](\w+)['\"]", m.group("args"))
        if renamed:
            name = renamed.group(1)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(src)
        out[name] = src[m.end():end]
    return out


def header_groups(src: str) -> tuple[set[str], set[str]]:
    """(READ names, WRITE names) from the module docstring's 'Tools exposed' block."""
    doc = src.split('"""', 2)[1]
    read_block = doc.split("READ:", 1)[1].split("WRITE", 1)[0]
    write_block = doc.split("WRITE", 1)[1]
    return set(_HEADER_LINE_RE.findall(read_block)), set(_HEADER_LINE_RE.findall(write_block))


def mutating_tools(tools: dict[str, str]) -> set[str]:
    out = set()
    for name, body in tools.items():
        calls = set(re.findall(r"_\w+_store\.(\w+)\(", body))
        if calls - READ_STORE_METHODS:
            out.add(name)
    return out


def main_turn_reads() -> set[str]:
    return {n.removeprefix(PREFIX) for n in runner.MAIN_TOOLS if n.startswith(PREFIX)}


def write_tools() -> set[str]:
    # ORC_WRITE_TOOLS spells the names with the mcp__orchestrator__ prefix; every entry must carry it.
    assert all(n.startswith(PREFIX) for n in runner.ORC_WRITE_TOOLS), runner.ORC_WRITE_TOOLS
    return {n.removeprefix(PREFIX) for n in runner.ORC_WRITE_TOOLS}


def test_server_file_parses_to_a_plausible_tool_set():
    """Guard on the parser itself: a silent empty parse would make every other test vacuously green."""
    tools = registered_tools(_source())
    assert len(tools) >= 15, sorted(tools)
    assert {"health", "get_canonical_memory", "apply_memory_update", "create_idea"} <= set(tools)


def test_every_registered_orc_tool_is_accounted_for():
    """Every tool the orchestrator server registers is one of: a main-turn read (MAIN_TOOLS), a
    disallowed write (ORC_WRITE_TOOLS), or an explicitly listed read the main turn does not get.
    A new tool of ANY kind fails here until someone classifies it."""
    registered = set(registered_tools(_source()))
    known = main_turn_reads() | write_tools() | NOT_FOR_MAIN_TURN
    unaccounted = sorted(registered - known)
    assert not unaccounted, (
        f"orchestrator MCP tool(s) registered but in neither MAIN_TOOLS, ORC_WRITE_TOOLS nor "
        f"NOT_FOR_MAIN_TURN: {unaccounted}. If it writes, add it to runner.ORC_WRITE_TOOLS; if it "
        f"reads, add it to runner.MAIN_TOOLS or to NOT_FOR_MAIN_TURN here.")


def test_no_stale_names_in_runner_lists():
    """Every name the runner lists (reads in MAIN_TOOLS, writes in ORC_WRITE_TOOLS) is actually
    registered: a renamed or removed server tool must not leave a dead entry behind, since a dead
    disallow entry means the real (renamed) write tool is no longer blocked."""
    registered = set(registered_tools(_source()))
    stale_reads = sorted(main_turn_reads() - registered)
    stale_writes = sorted(write_tools() - registered)
    stale_allow = sorted(NOT_FOR_MAIN_TURN - registered)
    assert not stale_reads, f"MAIN_TOOLS names no longer registered by the orchestrator: {stale_reads}"
    assert not stale_writes, f"ORC_WRITE_TOOLS names no longer registered by the orchestrator: {stale_writes}"
    assert not stale_allow, f"NOT_FOR_MAIN_TURN names no longer registered: {stale_allow}"
    assert not main_turn_reads() & write_tools(), "a tool is both a main-turn read and a disallowed write"


def test_header_classifies_every_registered_tool():
    """The header docstring READ/WRITE lists cover every registered tool (except the known gap)."""
    src = _source()
    reads, writes = header_groups(src)
    unlisted = sorted(set(registered_tools(src)) - reads - writes - KNOWN_HEADER_GAPS)
    assert not unlisted, f"registered but not in the mcp_server.py header READ/WRITE lists: {unlisted}"
    assert not reads & writes, f"listed as both READ and WRITE in the header: {sorted(reads & writes)}"
    gone = sorted((reads | writes) - set(registered_tools(src)))
    assert not gone, f"in the header but no longer registered: {gone}"


def test_write_set_is_exactly_orc_write_tools():
    """The server's write tools == ORC_WRITE_TOOLS, by three signals unioned:
      1. the header docstring's WRITE grouping (the server's own, human-maintained classification);
      2. any mutating store call in the tool body (`_<x>_store.<method>(` with a method outside the
         read set) — the mechanical ground truth, which catches a write tool left out of the header;
      3. a write verb at the start of the tool name — cheap, and catches a write that goes through a
         path other than a store (e.g. a direct file write).
    Also checked separately: the header WRITE list alone equals ORC_WRITE_TOOLS, so the header can't
    drift from the runner either way."""
    src = _source()
    tools = registered_tools(src)
    _, header_writes = header_groups(src)
    by_store = mutating_tools(tools)
    by_verb = {n for n in tools if n.startswith(WRITE_VERBS)}
    detected = header_writes | by_store | by_verb
    expected = write_tools()
    unblocked = sorted(detected - expected)
    assert not unblocked, (
        f"orchestrator WRITE tool(s) NOT in runner.ORC_WRITE_TOOLS (reachable by the main turn and "
        f"helpers): {unblocked}  [header={sorted(header_writes - expected)}, "
        f"store-mutation={sorted(by_store - expected)}, verb={sorted(by_verb - expected)}]")
    not_writes = sorted(expected - detected)
    assert not not_writes, (
        f"runner.ORC_WRITE_TOOLS names tool(s) no write signal recognises: {not_writes}")
    assert header_writes == expected, (
        f"header WRITE list != ORC_WRITE_TOOLS: header-only={sorted(header_writes - expected)}, "
        f"runner-only={sorted(expected - header_writes)}")
    # and no read tool trips a write signal (keeps the signals honest)
    reads = main_turn_reads() | NOT_FOR_MAIN_TURN
    assert not reads & by_store, f"read tool(s) call a mutating store method: {sorted(reads & by_store)}"


def test_helpers_get_every_read_and_block_every_write(monkeypatch):
    """A normal helper's options carry every orchestrator read name in `tools` and every
    ORC_WRITE_TOOLS name in `disallowed_tools`, cross-checked against the server's registered names
    (not only against the runner's own lists, which test_helpers already does)."""
    monkeypatch.delenv("CONDUCTOR_HELPER_ORC", raising=False)
    from navigator.conductor.helpers import helper_options
    src = _source()
    registered = set(registered_tools(src))
    _, header_writes = header_groups(src)
    opts = helper_options(hook=None, exact=None, helper_id=None)
    assert set(opts.mcp_servers) == {"orchestrator"}
    for name in main_turn_reads():
        assert PREFIX + name in opts.tools, f"helper missing orchestrator read tool {name}"
    for name in registered & (header_writes | mutating_tools(registered_tools(src))):
        assert PREFIX + name in opts.disallowed_tools, f"helper does NOT disallow write tool {name}"
    for full in runner.ORC_WRITE_TOOLS:
        assert full in opts.disallowed_tools and full not in opts.tools, full
    assert set(opts.disallowed_tools) == set(runner.ORC_WRITE_TOOLS)
