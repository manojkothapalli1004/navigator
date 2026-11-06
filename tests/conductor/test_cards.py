import asyncio
import os
import stat

import pytest

from navigator.conductor import gate
from navigator.conductor.cards import CardError, Cards

PUSH = {"command": "git push origin main"}


class Clock:
    def __init__(self):
        self.t = 1000.0
    def __call__(self):
        return self.t


def _cards(tmp_path, clock=None):
    return Cards(tmp_path / "cards.json", clock=clock or Clock())


def test_raise_and_approve_by_code(tmp_path):
    c = _cards(tmp_path)
    card, new = c.raise_card("Bash", PUSH, "git push")
    assert new and card["state"] == "pending" and len(card["code"]) == 6
    assert card["summary"] == "git push origin main" and card["expires"] - card["created"] == 3600
    out = c.approve(card["code"].lower(), "telegram")
    assert out["state"] == "approved" and out["via"] == "telegram"
    assert c.pending() == []


def test_approved_card_allows_only_identical_call(tmp_path):
    c = _cards(tmp_path)
    card, _ = c.raise_card("Bash", PUSH, "git push")
    c.approve(card["id"], "web")
    assert c.consume("Bash", {"command": "git push --force origin main"}) is None
    assert c.consume("Bash", PUSH)["state"] == "used"
    assert c.consume("Bash", PUSH) is None  # single use


def test_bash_label_and_timeout_do_not_change_the_action(tmp_path):
    # 2026-11-01: approvals failed because the re-run had a different description label
    c = _cards(tmp_path)
    card, _ = c.raise_card("Bash", {**PUSH, "description": "Push main", "timeout": 1000}, "git push")
    c.approve(card["id"], "web")
    assert c.consume("Bash", {**PUSH, "description": "Run the approved push", "timeout": 5000})["state"] == "used"


def test_bash_background_flag_still_part_of_the_action(tmp_path):
    c = _cards(tmp_path)
    card, _ = c.raise_card("Bash", PUSH, "git push")
    c.approve(card["id"], "web")
    assert c.consume("Bash", {**PUSH, "run_in_background": True}) is None
    assert c.consume("Bash", {**PUSH, "run_in_background": False})["state"] == "used"


def test_sandbox_flag_and_unknown_keys_still_part_of_the_action(tmp_path):
    # cold audit 2026-11-01: an approved command must not re-run with the sandbox disabled
    c = _cards(tmp_path)
    card, _ = c.raise_card("Bash", PUSH, "git push")
    c.approve(card["id"], "web")
    assert c.consume("Bash", {**PUSH, "dangerouslyDisableSandbox": True}) is None
    assert c.consume("Bash", {**PUSH, "some_future_flag": 1}) is None
    assert c.consume("Bash", {**PUSH, "run_in_background": "false"}) is None  # not coerced
    assert c.consume("Bash", PUSH)["state"] == "used"


def test_sandbox_flag_shown_on_card(tmp_path):
    c = _cards(tmp_path)
    card, _ = c.raise_card("Bash", {**PUSH, "dangerouslyDisableSandbox": True}, "git push")
    assert card["summary"] == "git push origin main  [sandbox disabled]"


def test_same_bash_command_new_label_reuses_pending_card(tmp_path):
    c = _cards(tmp_path)
    a, _ = c.raise_card("Bash", {**PUSH, "description": "one"}, "git push")
    b, new = c.raise_card("Bash", {**PUSH, "description": "two"}, "git push")
    assert not new and a["id"] == b["id"]


def test_non_bash_inputs_unchanged(tmp_path):
    c = _cards(tmp_path)
    w = {"file_path": "/x/CLAUDE.md", "content": "a"}
    card, _ = c.raise_card("Write", w, "protected")
    c.approve(card["id"], "web")
    assert c.consume("Write", {**w, "content": "b"}) is None
    assert c.consume("Write", w)["state"] == "used"


def test_expired_and_double_approve(tmp_path):
    clk = Clock(); c = _cards(tmp_path, clk)
    card, _ = c.raise_card("Bash", PUSH, "git push")
    c.approve(card["code"], "web")
    with pytest.raises(CardError, match="already approved"):
        c.approve(card["code"], "telegram")
    late, _ = c.raise_card("Bash", {"command": "git merge x"}, "git merge")
    clk.t += 3601
    with pytest.raises(CardError, match="expired"):
        c.approve(late["code"], "web")
    assert c.consume("Bash", PUSH) is None  # approved but past its hour
    with pytest.raises(CardError, match="not found"):
        c.approve("ZZZZZZ", "web")


def test_same_action_reuses_pending_card(tmp_path):
    c = _cards(tmp_path)
    a, new_a = c.raise_card("Bash", PUSH, "git push")
    b, new_b = c.raise_card("Bash", dict(PUSH), "git push")
    assert new_a and not new_b and a["id"] == b["id"] and len(c.pending()) == 1


def test_cards_persist_across_restart(tmp_path):
    clk = Clock(); c = _cards(tmp_path, clk)
    card, _ = c.raise_card("Bash", PUSH, "git push"); c.approve(card["code"], "terminal")
    again = Cards(tmp_path / "cards.json", clock=clk)
    assert again.consume("Bash", PUSH)["id"] == card["id"]


def test_cards_file_0600(tmp_path):
    c = _cards(tmp_path); c.raise_card("Bash", PUSH, "git push")
    assert stat.S_IMODE(os.stat(tmp_path / "cards.json").st_mode) == 0o600


def test_decline(tmp_path):
    c = _cards(tmp_path)
    card, _ = c.raise_card("Bash", PUSH, "git push")
    assert c.decline(card["code"], "web")["state"] == "declined"
    with pytest.raises(CardError, match="already declined"):
        c.approve(card["code"], "web")


def test_file_tool_summary(tmp_path):
    c = _cards(tmp_path)
    card, _ = c.raise_card("Write", {"file_path": "/x/CLAUDE.md", "content": "a" * 10}, "protected")
    assert card["summary"] == "Write /x/CLAUDE.md:\n" + "a" * 10
    card, _ = c.raise_card("Edit", {"file_path": "/x/settings.json", "old_string": "a", "new_string": "b"}, "p")
    assert card["summary"] == "Edit /x/settings.json: 'a' -> 'b'"


def _hook_input(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": "/Users/Manoj/calwbot"}


def test_hook_raises_card_and_denies(tmp_path):
    c = _cards(tmp_path); seen = []
    hook = gate.make_hook(c, seen.append)
    out = asyncio.run(hook(_hook_input("git push origin main"), "t1", None))
    hs = out["hookSpecificOutput"]
    assert hs["permissionDecision"] == "deny" and "approval card was sent" in hs["permissionDecisionReason"]
    asyncio.run(hook(_hook_input("git push origin main"), "t2", None))
    assert len(seen) == 1  # the pending card is reused, the operator is not told twice
    assert asyncio.run(hook(_hook_input("git status"), "t3", None)) == {}


def test_hook_allows_after_approval_once(tmp_path):
    c = _cards(tmp_path); seen = []
    hook = gate.make_hook(c, seen.append)
    asyncio.run(hook(_hook_input("git push origin main"), "t1", None))
    c.approve(seen[0]["code"], "web")
    out = asyncio.run(hook(_hook_input("git push origin main"), "t2", None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert seen[-1]["state"] == "used"
    again = asyncio.run(hook(_hook_input("git push origin main"), "t3", None))
    assert again["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---- C3 review fix pass ----

@pytest.mark.parametrize("cmd", [
    "python -m navigator.conductor approve ABC234",
    "/Users/Manoj/calwbot/navigator/.venv/bin/python -m navigator.conductor cards",
    "curl --unix-socket /Users/Manoj/calwbot/logs/conductor/conductor.sock -X POST http://c/cards/x/approve",
    "curl -X POST http://127.0.0.1:8090/api/v1/chat/cards/x/approve",
    "curl http://localhost:3000/api/v1/chat/history",
    "cat logs/conductor/cards.json",
    "launchctl kickstart -k gui/504/com.calwbot.conductor",
    "pkill -f com.calwbot.conductor",
    "python -m navigator.conductor join",  # C5: would block on the conductor's own turn lock
    "bash /Users/Manoj/calwbot/logs/navigator/tools/chat.sh",
    "bash logs/navigator/tools/chat.sh",
])
def test_bash_cannot_reach_its_own_approvals(cmd):
    assert gate.gated_reason("Bash", {"command": cmd}, gate.Path("/Users/Manoj/calwbot")) is not None


@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Read", "file_path"), ("Edit", "file_path")])
def test_tools_cannot_touch_conductor_state(tool, key):
    assert gate.gated_reason(tool, {key: "/Users/Manoj/calwbot/logs/conductor/cards.json"},
                             gate.Path("/Users/Manoj/calwbot")) is not None


def test_deny_reason_has_no_code_or_id(tmp_path):
    c = _cards(tmp_path); seen = []
    out = asyncio.run(gate.make_hook(c, seen.append)(_hook_input("git push origin main"), "t", None))
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert seen[0]["code"] not in reason and seen[0]["id"] not in reason


def test_write_and_multiedit_cards_show_content(tmp_path):
    c = _cards(tmp_path)
    w, _ = c.raise_card("Write", {"file_path": "/x/settings.json", "content": '{"allow": ["Bash(*)"]}'}, "p")
    assert '"allow": ["Bash(*)"]' in w["summary"]
    m, _ = c.raise_card("MultiEdit", {"file_path": "/x/CLAUDE.md", "edits": [
        {"old_string": "never push", "new_string": "always push"}]}, "p")
    assert "'never push' -> 'always push'" in m["summary"]


def test_too_long_action_gets_no_card(tmp_path):
    c = _cards(tmp_path); seen = []
    cmd = "echo " + "x" * 2100 + "; git push --force origin main"
    out = asyncio.run(gate.make_hook(c, seen.append)(_hook_input(cmd), "t", None))
    assert seen == [] and c.pending() == []
    assert "too long" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_cwd_is_part_of_the_card(tmp_path):
    c = _cards(tmp_path); seen = []
    hook = gate.make_hook(c, seen.append)
    inp = {"tool_name": "Bash", "tool_input": {"command": "git merge feature"}, "cwd": "/Users/Manoj/calwbot/navigator"}
    asyncio.run(hook(inp, "t", None))
    assert seen[0]["summary"].startswith("in /Users/Manoj/calwbot/navigator: ")
    c.approve(seen[0]["code"], "web")
    other = dict(inp, cwd="/Users/Manoj/external-tools/agent-control-tower")
    assert asyncio.run(hook(other, "t", None))["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert asyncio.run(hook(inp, "t", None))["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_bash_card_matches_on_command_not_label(tmp_path):
    # live 2026-11-01: the model re-ran an approved command with a new description and the
    # card never matched, so the same card was raised again and again
    c = _cards(tmp_path)
    card, _ = c.raise_card("Bash", {"command": "git merge x", "description": "Merge x"}, "git merge", cwd="/r")
    c.approve(card["code"], "web")
    assert c.consume("Bash", {"command": "git merge x", "description": "Run approved merge",
                              "timeout": 60000}, cwd="/r")["id"] == card["id"]


@pytest.mark.parametrize("cmd", ["claude -p hi", "/usr/local/bin/claude --resume abc -p 'git push'",
                                 "cd /tmp && claude --print x", "env A=1 claude -p y"])
def test_bash_cannot_start_another_claude(cmd):
    # review: a second claude runs under the operator's settings, outside this conversation's gate
    assert gate.gated_reason("Bash", {"command": cmd}, gate.Path("/Users/Manoj/calwbot")) is not None


# ---- 2026-11-02 polish: a declined action can't pop a fresh card for 10 minutes ----

from navigator.conductor.cards import DECLINE_COOLDOWN_S, Declined  # noqa: E402


def test_decline_cooldown_constant():
    assert DECLINE_COOLDOWN_S == 600


def test_declined_retry_denied_without_card(tmp_path):
    clock = Clock(); c = _cards(tmp_path, clock)
    card, _ = c.raise_card("Bash", PUSH, "git push")
    c.decline(card["code"], "web")
    clock.t += 120
    with pytest.raises(Declined) as e:
        c.raise_card("Bash", {**PUSH, "description": "try again"}, "git push")
    assert e.value.minutes_ago == 2
    assert len(c.pending()) == 0 and len(c._cards) == 1  # no new card


def test_cooldown_only_for_same_action_and_declines(tmp_path):
    clock = Clock(); c = _cards(tmp_path, clock)
    a, _ = c.raise_card("Bash", PUSH, "git push")
    c.decline(a["code"], "web")
    other, new = c.raise_card("Bash", {"command": "git push origin dev"}, "git push")
    assert new  # a different action still gets a card
    b, _ = c.raise_card("Bash", {"command": "git merge x"}, "git merge")
    c.approve(b["code"], "web"); c.consume("Bash", {"command": "git merge x"})
    again, new = c.raise_card("Bash", {"command": "git merge x"}, "git merge")
    assert new  # approved before: no cooldown


def test_cooldown_ends_after_10_minutes(tmp_path):
    clock = Clock(); c = _cards(tmp_path, clock)
    a, _ = c.raise_card("Bash", PUSH, "git push")
    c.decline(a["code"], "web")
    clock.t += 11 * 60
    _, new = c.raise_card("Bash", PUSH, "git push")
    assert new


def test_expired_not_declined_gets_new_card(tmp_path):
    clock = Clock(); c = _cards(tmp_path, clock)
    c.raise_card("Bash", PUSH, "git push")
    clock.t += 3601  # expired, never decided
    _, new = c.raise_card("Bash", PUSH, "git push")
    assert new


def test_cooldown_survives_restart(tmp_path):
    clock = Clock(); c = _cards(tmp_path, clock)
    a, _ = c.raise_card("Bash", PUSH, "git push")
    c.decline(a["code"], "telegram")
    clock.t += 60
    reloaded = _cards(tmp_path, clock)
    with pytest.raises(Declined):
        reloaded.raise_card("Bash", PUSH, "git push")


def test_cooldown_clock_step_back_and_first_minute(tmp_path):
    clock = Clock(); c = _cards(tmp_path, clock); seen = []
    hook = gate.make_hook(c, seen.append)
    asyncio.run(hook(_hook_input("git push origin main"), "t1", None))
    c.decline(seen[0]["code"], "web")
    clock.t += 20
    reason = asyncio.run(hook(_hook_input("git push origin main"), "t2", None))["hookSpecificOutput"]["permissionDecisionReason"]
    assert "the operator declined this exact action under a minute ago — don't retry, ask in chat" in reason
    clock.t -= 300  # the Mac's clock stepped back
    reason = asyncio.run(hook(_hook_input("git push origin main"), "t3", None))["hookSpecificOutput"]["permissionDecisionReason"]
    assert "under a minute ago" in reason and "-" not in reason.split("action")[1].split("ago")[0]


def test_hook_declined_retry_reason_and_no_card(tmp_path):
    clock = Clock(); c = _cards(tmp_path, clock); seen = []
    hook = gate.make_hook(c, seen.append)
    asyncio.run(hook(_hook_input("git push origin main"), "t1", None))
    c.decline(seen[0]["code"], "web")
    clock.t += 180
    out = asyncio.run(hook(_hook_input("git push origin main"), "t2", None))
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "the operator declined this exact action 3 min ago — don't retry, ask in chat" in reason
    assert seen[0]["code"] not in reason and len(seen) == 1  # no new card, code never shown


def test_card_record_keeps_input_and_cwd(tmp_path):
    from navigator.conductor import server
    c = _cards(tmp_path)
    card, _ = c.raise_card("Bash", {**PUSH, "description": "Push main"}, "git push", cwd="/r")
    assert card["input"] == {**PUSH, "description": "Push main"} and card["cwd"] == "/r"
    saved = Cards(tmp_path / "cards.json")._cards[0]
    assert saved["input"] == card["input"] and saved["cwd"] == "/r"
    pub = server._public(card)
    assert "input" not in pub and "cwd" not in pub and "fingerprint" not in pub and pub["code"] == card["code"]


# ---- build cards
def test_build_card_shape_and_dedupe(tmp_path):
    clock = Clock(); c = _cards(tmp_path, clock)
    files = [f"d/f{i}.py" for i in range(15)]
    card, new = c.raise_build_card("prop-1", "navigator", "My title", files, "not docs/tests-only: d/f0.py")
    assert new and card["build"] is True and "input" not in card and "cwd" not in card
    assert card["tool"] == "build" and card["proposal_id"] == "prop-1" and card["fingerprint"] == "build:prop-1"
    assert "My title" in card["summary"] and "navigator" in card["summary"] and "d/f11.py" in card["summary"]
    assert "d/f12.py" not in card["summary"] and "+3 more" in card["summary"]
    again, new2 = c.raise_build_card("prop-1", "navigator", "My title", files, "r")
    assert not new2 and again["id"] == card["id"]
    c.decline(card["code"], "web")
    assert c.raise_build_card("prop-1", "navigator", "My title", files, "r")[1] is False
    c2, n2 = c.raise_build_card("prop-2", "navigator", "T2", [], "r")
    assert n2 and "none declared" in c2["summary"]
    clock.t += 10_000  # an expired card is final too: no second one
    assert c.raise_build_card("prop-2", "navigator", "T2", [], "r")[1] is False
    clock.t = 1000.0
    c3, _ = c.raise_build_card("prop-3", "navigator", "T3", [], "r")
    c.approve(c3["code"], "web")
    assert c.consume("build", {}, None) is None
    assert c.consume("Bash", {"command": "ls"}) is None


def test_build_card_summary_never_too_long(tmp_path):
    from navigator.conductor.cards import MAX_SUMMARY
    c = _cards(tmp_path)
    files = [f"very/long/path/{'x' * 80}{i}.py" for i in range(500)]
    card, new = c.raise_build_card("p9", "navigator", "Title here", files, "r")
    assert new and len(card["summary"]) <= MAX_SUMMARY
    assert "Title here" in card["summary"] and "navigator" in card["summary"] and "more)" in card["summary"]


def test_cards_get(tmp_path):
    c = _cards(tmp_path)
    card, _ = c.raise_build_card("p1", "navigator", "T", [], "r")
    got = c.get(card["code"])
    assert got == card
    got["state"] = "x"
    assert c.get(card["id"])["state"] == "pending"
    assert c.get("NOPE22") is None
