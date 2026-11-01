from navigator.checker.checks import Finding, check_page


def snap(**over):
    base = {
        "page": "/", "viewport": "phone", "status": 200, "console_errors": [], "failed_requests": [],
        "scroll_width": 412, "viewport_width": 412, "small_targets": [], "live_claims": [], "combos": [],
        "interactions": {},
    }
    base.update(over)
    return base


NOW = 1_800_000_000.0


def checks_of(findings):
    return [f.check for f in findings]


def test_fingerprint_stable_and_distinct():
    a = Finding("/", "phone", "hscroll", "body", "x")
    b = Finding("/", "phone", "hscroll", "body", "different detail")
    c = Finding("/", "phone", "hscroll", "table", "x")
    assert a.fingerprint == b.fingerprint
    assert len(a.fingerprint) == 16 and all(ch in "0123456789abcdef" for ch in a.fingerprint)
    assert a.fingerprint != c.fingerprint


def test_hscroll_only_when_wider():
    assert checks_of(check_page(snap(scroll_width=413), NOW)) == ["hscroll"]
    assert check_page(snap(scroll_width=412), NOW) == []


def test_tap_size_phone_only():
    t = [{"label": "MORE", "w": 36, "h": 25}]
    assert checks_of(check_page(snap(small_targets=t), NOW)) == ["tap-size"]
    assert check_page(snap(viewport="desktop", viewport_width=1440, scroll_width=1440, small_targets=t), NOW) == []


def test_allowed_404_ignored():
    req = ["404 http://127.0.0.1:3000/api/v1/agent/docs/navigator%2Fsettings"]
    assert check_page(snap(failed_requests=req), NOW) == []
    assert checks_of(check_page(snap(failed_requests=["500 http://127.0.0.1:3000/api/v1/x"]), NOW)) == ["request"]


def test_console_error_is_a_finding():
    assert checks_of(check_page(snap(console_errors=["boom"]), NOW)) == ["console"]


def test_impossible_number():
    combos = [{"combo": "momentum-BTC", "total_pnl_usd": 762820.58, "initial_capital": 1000},
              {"combo": "rsi-ETH", "total_pnl_usd": -4.03, "initial_capital": 1000}]
    found = check_page(snap(page="/trader", combos=combos), NOW)
    assert checks_of(found) == ["impossible-number"]
    assert found[0].element == "momentum-BTC"


def test_stale_live():
    claim = lambda age: [{"label": "trader", "captured_at": NOW - age, "poll_ms": 15000}]
    assert checks_of(check_page(snap(live_claims=claim(60)), NOW)) == ["stale-live"]
    assert check_page(snap(live_claims=claim(30)), NOW) == []


def test_interaction_false():
    found = check_page(snap(interactions={"menu-open": False, "folds-open": True}), NOW)
    assert checks_of(found) == ["interaction"]
    assert found[0].element == "menu-open"


def test_load_failure_short_circuits():
    found = check_page(snap(status=None, scroll_width=900, console_errors=["x"]), NOW)
    assert checks_of(found) == ["load"]


def test_generic_resource_404_console_line_left_to_request_check():
    msg = "Failed to load resource: the server responded with a status of 404 (Not Found)"
    req = ["404 http://127.0.0.1:3000/api/v1/agent/docs/navigator%2Fsettings"]
    assert check_page(snap(console_errors=[msg], failed_requests=req), NOW) == []
