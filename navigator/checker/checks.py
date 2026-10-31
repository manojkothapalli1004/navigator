"""Pure checks over one page snapshot (no browser here): each fault is a Finding with a stable fingerprint."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal, TypedDict

# 404 while no settings document has been saved yet: the page handles it, the browser logs it
ALLOWED_FAILED = ("/api/v1/agent/docs/navigator%2Fsettings",)
MIN_TAP_PX = 40
IMPOSSIBLE_PNL_X = 100   # p&l larger than this many times the starting capital is a data bug
STALE_POLLS = 3


@dataclass(frozen=True)
class Finding:
    page: str
    viewport: Literal["phone", "desktop"]
    check: str
    element: str
    detail: str
    screenshot: str | None = None

    @property
    def fingerprint(self) -> str:
        key = f"{self.page}|{self.viewport}|{self.check}|{self.element}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]


class PageSnapshot(TypedDict):
    page: str
    viewport: Literal["phone", "desktop"]
    status: int | None
    console_errors: list[str]
    failed_requests: list[str]
    scroll_width: int
    viewport_width: int
    small_targets: list[dict]      # {label, w, h}
    live_claims: list[dict]        # {label, captured_at (epoch s), poll_ms}
    combos: list[dict]             # {combo, total_pnl_usd, initial_capital}
    interactions: dict[str, bool]  # name -> worked


def check_page(snap: PageSnapshot, now: float, screenshot: str | None = None) -> list[Finding]:
    """Every fault on one page at one viewport. A page that did not load reports only that."""
    page, vp = snap["page"], snap["viewport"]

    def f(check: str, element: str, detail: str) -> Finding:
        return Finding(page, vp, check, element, detail, screenshot)

    status = snap.get("status")
    if status is None or status >= 400:
        return [f("load", "page", f"page did not load (status {status})")]

    out: list[Finding] = []
    for msg in snap.get("console_errors", []):
        out.append(f("console", msg[:80], msg))
    for req in snap.get("failed_requests", []):
        if not any(allowed in req for allowed in ALLOWED_FAILED):
            out.append(f("request", req[:120], req))
    if snap["scroll_width"] > snap["viewport_width"]:
        out.append(f("hscroll", "body", f"page is {snap['scroll_width']}px wide on a {snap['viewport_width']}px screen"))
    if vp == "phone":
        for t in snap.get("small_targets", []):
            if t["w"] < MIN_TAP_PX or t["h"] < MIN_TAP_PX:
                out.append(f("tap-size", str(t["label"])[:40], f"{t['w']}x{t['h']}px, needs {MIN_TAP_PX}px"))
    for c in snap.get("combos", []):
        cap = float(c.get("initial_capital") or 0)
        pnl = float(c.get("total_pnl_usd") or 0)
        if cap > 0 and abs(pnl) > IMPOSSIBLE_PNL_X * cap:
            out.append(f("impossible-number", c["combo"], f"p&l {pnl:.2f} on capital {cap:.0f}"))
    for claim in snap.get("live_claims", []):
        age = now - float(claim["captured_at"])
        if age > STALE_POLLS * float(claim["poll_ms"]) / 1000:
            out.append(f("stale-live", claim["label"], f"data is {age:.0f}s old"))
    for name, ok in snap.get("interactions", {}).items():
        if ok is False:
            out.append(f("interaction", name, f"{name} did not work"))
    return out
