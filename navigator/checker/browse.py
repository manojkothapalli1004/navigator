"""Drive the installed Google Chrome through the console and turn each page into a PageSnapshot.

playwright is an optional extra (`--extra checker`); it is imported lazily so the rest of navigator
never needs it."""
from __future__ import annotations

import contextlib
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from navigator.checker.checks import PageSnapshot

VIEWPORTS = {
    "phone": dict(viewport={"width": 412, "height": 915}, device_scale_factor=2, is_mobile=True, has_touch=True),
    "desktop": dict(viewport={"width": 1440, "height": 900}),
}
LOAD_TIMEOUT_MS = 30_000
SETTLE_MS = 2_500

# overflow, tap targets and "live" claims, measured in the page
MEASURE_JS = """() => {
  const vw = document.documentElement.clientWidth;
  const small = [];
  for (const el of document.querySelectorAll('a,button,summary,select,input')) {
    const r = el.getBoundingClientRect();
    if (r.width && r.height && r.top < 3000)
      small.push({label: (el.innerText || el.getAttribute('aria-label') || el.tagName).trim().slice(0, 40),
                  w: Math.round(r.width), h: Math.round(r.height)});
  }
  const live = [...document.querySelectorAll('[data-captured-at][data-poll-ms]')].map(el => ({
    label: el.getAttribute('data-label') || el.textContent.trim().slice(0, 30),
    captured_at: el.getAttribute('data-captured-at'), poll_ms: Number(el.getAttribute('data-poll-ms'))}));
  return {scroll_width: document.documentElement.scrollWidth, viewport_width: vw, small_targets: small, live_claims: live};
}"""


class BrowserUnavailable(RuntimeError):
    """playwright is not installed or Chrome would not launch."""


class ConsoleUnreachable(RuntimeError):
    """The console's front page did not answer."""


@contextlib.contextmanager
def open_browser():
    """A headless Google Chrome (channel="chrome"); BrowserUnavailable when that is impossible."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise BrowserUnavailable(f"playwright not installed: {e}") from e
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as e:  # playwright raises its own Error type for a missing channel
            raise BrowserUnavailable(str(e)) from e
        try:
            yield browser
        finally:
            browser.close()


def _epoch(iso: str | None) -> float:
    """Seconds since the epoch; naive timestamps are UTC (ACT writes naive UTC). 0 when unparseable."""
    if not iso:
        return 0.0
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.timestamp()


def discover_pages(browser, base: str) -> list[str]:
    """Same-site paths linked from the front page's <nav>, in order, without duplicates."""
    ctx = browser.new_context(**VIEWPORTS["desktop"])
    try:
        page = ctx.new_page()
        try:
            page.goto(base + "/", wait_until="networkidle", timeout=LOAD_TIMEOUT_MS)
        except Exception as e:
            raise ConsoleUnreachable(f"{base}: {e}") from e
        hrefs = page.eval_on_selector_all("nav a[href]", "els => els.map(e => e.getAttribute('href'))")
    finally:
        ctx.close()
    out: list[str] = []
    host = urlparse(base).netloc
    for href in hrefs:
        url = urlparse(urljoin(base + "/", href))
        if url.netloc != host:
            continue
        path = url.path or "/"
        if path not in out:
            out.append(path)
    return out


def _interactions(page, viewport: str) -> dict[str, bool]:
    result: dict[str, bool] = {}
    if viewport == "phone":
        btn = page.locator('button[aria-label="Open menu"]')
        if btn.count():
            btn.first.click()
            page.wait_for_timeout(400)
            # the desktop rail is a <nav> too, hidden on phones: only a visible one counts
            opened = page.locator("nav:visible").count() > 0
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            result["menu-open"] = bool(opened)
    folds = page.locator("details > summary")
    if folds.count():
        ok = True
        for i in range(min(folds.count(), 4)):
            summary = folds.nth(i)
            before = summary.evaluate("s => s.parentElement.open")
            try:
                summary.click(timeout=2_000)
            except Exception:
                ok = False
                continue
            if summary.evaluate("s => s.parentElement.open") == before:
                ok = False
        result["folds-open"] = ok
    group = page.locator('[aria-label="outcome"] button[aria-pressed="false"]')
    if group.count():
        # a filter works when tapping it selects it (the row count may legitimately stay the same)
        target = group.first
        label = target.inner_text()
        target.click()
        page.wait_for_timeout(300)
        chosen = page.locator('[aria-label="outcome"] button', has_text=label).first
        result["filter-changes"] = chosen.get_attribute("aria-pressed") == "true"
    return result


def _slug(path: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", path.lower()).strip("_") or "home"


def snapshot(browser, base: str, path: str, viewport: str, out_dir: Path) -> tuple[PageSnapshot, str]:
    """Load one page at one viewport, measure it, exercise menu/folds/filters, screenshot it."""
    ctx = browser.new_context(**VIEWPORTS[viewport])
    console: list[str] = []
    failed: list[str] = []
    try:
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and console.append(m.text[:300]))
        page.on("pageerror", lambda e: console.append(f"pageerror: {str(e)[:300]}"))
        page.on("requestfailed", lambda r: failed.append(f"failed {r.url[:200]}"))
        page.on("response", lambda r: r.status >= 400 and failed.append(f"{r.status} {r.url[:200]}"))
        status: int | None
        try:
            resp = page.goto(base + path, wait_until="networkidle", timeout=LOAD_TIMEOUT_MS)
            status = resp.status if resp else None
        except Exception:
            status = None
        out_dir.mkdir(parents=True, exist_ok=True)
        shot = str(out_dir / f"{viewport}__{_slug(path)}.png")
        measured = {"scroll_width": 0, "viewport_width": 0, "small_targets": [], "live_claims": []}
        interactions: dict[str, bool] = {}
        combos: list[dict] = []
        if status is not None:
            page.wait_for_timeout(SETTLE_MS)
            page.screenshot(path=shot, full_page=True)
            measured = page.evaluate(MEASURE_JS)
            interactions = _interactions(page, viewport)
            if path.rstrip("/") == "/trader" and viewport == "desktop":   # data, not layout: once is enough
                combos = _combos(page, base)
        snap: PageSnapshot = {
            "page": path, "viewport": viewport, "status": status,
            "console_errors": console, "failed_requests": failed,
            "scroll_width": measured["scroll_width"], "viewport_width": measured["viewport_width"],
            "small_targets": measured["small_targets"],
            "live_claims": [dict(c, captured_at=_epoch(c["captured_at"])) for c in measured["live_claims"]],
            "combos": combos, "interactions": interactions,
        }
        return snap, shot
    finally:
        ctx.close()


def _combos(page, base: str) -> list[dict]:
    """Per-combo lifetime p&l with its starting capital, from the trader probe."""
    try:
        r = page.request.get(base + "/api/v1/sources/trader/probe", timeout=10_000)
        payload = r.json().get("payload", {}) if r.ok else {}
    except Exception:
        return []
    return [{"combo": c.get("combo", "?"), "total_pnl_usd": c.get("total_pnl_usd", 0),
             "initial_capital": c.get("initial_capital", 1000)} for c in payload.get("by_combo", [])]


def browse(base: str, out_dir: Path, browser=None) -> list[tuple[PageSnapshot, str]]:
    """Every menu page at phone and desktop size. ConsoleUnreachable when the front page does not answer."""
    if browser is None:
        with open_browser() as b:
            return browse(base, out_dir, browser=b)
    pages = discover_pages(browser, base)
    return [snapshot(browser, base, path, vp, out_dir) for path in pages for vp in ("phone", "desktop")]
