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
    assert card["summary"] == "Write /x/CLAUDE.md (10 chars)"
    card, _ = c.raise_card("Edit", {"file_path": "/x/settings.json", "old_string": "a", "new_string": "b"}, "p")
    assert card["summary"] == "Edit /x/settings.json: 'a' -> 'b'"


def _hook_input(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": "/Users/Manoj/calwbot"}


def test_hook_raises_card_and_denies(tmp_path):
    c = _cards(tmp_path); seen = []
    hook = gate.make_hook(c, seen.append)
    out = asyncio.run(hook(_hook_input("git push origin main"), "t1", None))
    hs = out["hookSpecificOutput"]
    assert hs["permissionDecision"] == "deny" and seen[0]["code"] in hs["permissionDecisionReason"]
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
