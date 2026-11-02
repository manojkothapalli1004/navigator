"""Autopilot (2026-11-02): keep working through the operator's plan file between turns, with guardrails."""
from datetime import datetime
from zoneinfo import ZoneInfo

from navigator.conductor.autopilot import MAX_CONSECUTIVE, MAX_PER_DAY, Autopilot

ET = ZoneInfo("America/New_York")
NOON = datetime(2026, 11, 2, 12, 0, tzinfo=ET)


class Clock:
    def __init__(self, t=NOON):
        self.t = t
    def __call__(self):
        return self.t


PLAN = "# Autopilot plan\n- [x] small fixes\n- [ ] shadow mode\n- [ ] second-opinion audits\n"


def _ap(tmp_path, plan=PLAN, clock=None):
    (tmp_path / "plan.md").write_text(plan)
    return Autopilot(tmp_path / "state.json", tmp_path / "plan.md", clock=clock or Clock())


def test_defaults():
    assert MAX_CONSECUTIVE == 6 and MAX_PER_DAY == 12


def test_off_by_default_and_survives_restart(tmp_path):
    ap = _ap(tmp_path)
    assert ap.on is False
    assert ap.decide("done", cards_pending=False) == ("idle", None)
    ap.set(True)
    assert Autopilot(tmp_path / "state.json", tmp_path / "plan.md", clock=Clock()).on is True


def test_queues_next_unchecked_item_after_a_normal_turn(tmp_path):
    ap = _ap(tmp_path); ap.set(True)
    kind, item = ap.decide("done", cards_pending=False)
    assert (kind, item) == ("queue", "shadow mode")


def test_never_after_error_or_timeout_and_waits(tmp_path):
    ap = _ap(tmp_path); ap.set(True)
    assert ap.decide("error", cards_pending=False)[0] == "wait"
    assert ap.decide("timeout", cards_pending=False)[0] == "wait"
    assert ap.on is True  # still on: continues after the operator's next normal turn


def test_stop_turns_it_off(tmp_path):
    ap = _ap(tmp_path); ap.set(True)
    kind, reason = ap.decide("cancelled", cards_pending=False)
    assert kind == "off" and ap.on is False and "stopped" in reason


def test_waits_while_a_card_is_pending(tmp_path):
    ap = _ap(tmp_path); ap.set(True)
    assert ap.decide("done", cards_pending=True)[0] == "wait"


def test_consecutive_cap_turns_it_off(tmp_path):
    ap = _ap(tmp_path); ap.set(True)
    for _ in range(MAX_CONSECUTIVE):
        assert ap.decide("done", cards_pending=False)[0] == "queue"
        ap.started()
    kind, reason = ap.decide("done", cards_pending=False)
    assert kind == "off" and ap.on is False and "6 autopilot turns in a row" in reason


def test_operator_turn_resets_consecutive(tmp_path):
    ap = _ap(tmp_path); ap.set(True)
    for _ in range(5):
        ap.started()
    ap.operator_turn()
    assert ap.decide("done", cards_pending=False)[0] == "queue"
    assert ap.state["consecutive"] == 0


def test_daily_cap_turns_it_off_and_resets_next_day(tmp_path):
    clock = Clock(); ap = _ap(tmp_path, clock=clock); ap.set(True)
    for _ in range(MAX_PER_DAY):
        ap.started(); ap.operator_turn()  # keep the in-a-row cap out of the way
    kind, reason = ap.decide("done", cards_pending=False)
    assert kind == "off" and "12 autopilot turns today" in reason
    clock.t = NOON.replace(day=3); ap.set(True)
    assert ap.decide("done", cards_pending=False)[0] == "queue"


def test_quiet_hours_wait_unless_allowed(tmp_path):
    clock = Clock(NOON.replace(hour=23, minute=30)); ap = _ap(tmp_path, clock=clock); ap.set(True)
    assert ap.decide("done", cards_pending=False)[0] == "wait"
    clock.t = NOON.replace(hour=6, minute=59)
    assert ap.decide("done", cards_pending=False)[0] == "wait"
    clock.t = NOON.replace(hour=7, minute=0)
    assert ap.decide("done", cards_pending=False)[0] == "queue"
    clock.t = NOON.replace(hour=2)
    ap.allow_quiet(True)
    assert ap.decide("done", cards_pending=False)[0] == "queue"


def test_plan_done_turns_it_off(tmp_path):
    ap = _ap(tmp_path, plan="- [x] a\n- [X] b\n"); ap.set(True)
    kind, reason = ap.decide("done", cards_pending=False)
    assert kind == "off" and ap.on is False and "plan is done" in reason


def test_missing_plan_file_turns_it_off(tmp_path):
    ap = Autopilot(tmp_path / "state.json", tmp_path / "none.md", clock=Clock()); ap.set(True)
    kind, reason = ap.decide("done", cards_pending=False)
    assert kind == "off" and "plan file" in reason


def test_command_words():
    assert Autopilot.command("autopilot on") == "on"
    assert Autopilot.command("  Autopilot OFF ") == "off"
    assert Autopilot.command("autopilot status") == "status"
    assert Autopilot.command("autopilot quiet ok") == "quiet_ok"
    assert Autopilot.command("autopilot on please and also build x") is None
    assert Autopilot.command("turn autopilot on") is None


def test_malformed_state_file_is_ignored(tmp_path):
    (tmp_path / "plan.md").write_text(PLAN)
    for bad in ("5", "[1,2]", '{"on": "false"}', "not json"):
        (tmp_path / "state.json").write_text(bad)
        ap = Autopilot(tmp_path / "state.json", tmp_path / "plan.md", clock=Clock())
        assert ap.on is False, bad


def test_cap_message_says_how_to_restart(tmp_path):
    ap = _ap(tmp_path); ap.set(True)
    for _ in range(MAX_CONSECUTIVE):
        ap.started()
    _, reason = ap.decide("done", cards_pending=False)
    assert reason == "6 autopilot turns in a row (say 'autopilot on' to continue)"


def test_runnable_recheck(tmp_path):
    clock = Clock(); ap = _ap(tmp_path, clock=clock); ap.set(True)
    assert ap.runnable(cards_pending=False) is None
    assert ap.runnable(cards_pending=True) == "an approval card is waiting for you"
    clock.t = NOON.replace(hour=23, minute=5)
    assert "quiet hours" in ap.runnable(cards_pending=False)
    ap.set(False)
    assert ap.runnable(cards_pending=False) == "autopilot is off"
