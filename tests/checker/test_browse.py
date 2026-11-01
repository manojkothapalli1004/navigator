import functools
import http.server
import socket
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser
from navigator.checker import browse  # noqa: E402

SITE = Path(__file__).parent / "fixtures" / "site"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def site():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def chrome():
    try:
        with browse.open_browser() as b:
            yield b
    except browse.BrowserUnavailable as e:
        pytest.skip(f"Chrome not available: {e}")


def test_discover_pages_from_nav(site, chrome, tmp_path):
    assert browse.discover_pages(chrome, site) == ["/", "/a.html"]


def test_snapshot_detects_overflow_and_console_error(site, chrome, tmp_path):
    snap, shot = browse.snapshot(chrome, site, "/a.html", "phone", tmp_path)
    assert snap["status"] == 200
    assert snap["scroll_width"] > snap["viewport_width"]
    assert any("boom" in m for m in snap["console_errors"])
    assert Path(shot).exists()


def test_unreachable_raises(chrome, tmp_path):
    with pytest.raises(browse.ConsoleUnreachable):
        browse.browse(f"http://127.0.0.1:{_free_port()}", tmp_path, browser=chrome)


def test_menu_open_sees_the_drawer_not_the_hidden_rail(site, chrome, tmp_path):
    snap, _ = browse.snapshot(chrome, site, "/drawer.html", "phone", tmp_path)
    assert snap["interactions"]["menu-open"] is True


def test_combos_only_read_on_desktop(site, chrome, tmp_path, monkeypatch):
    monkeypatch.setattr(browse, "_combos", lambda page, base: [{"combo": "x", "total_pnl_usd": 1, "initial_capital": 1}])
    phone, _ = browse.snapshot(chrome, site, "/trader/", "phone", tmp_path)
    desk, _ = browse.snapshot(chrome, site, "/trader/", "desktop", tmp_path)
    assert phone["combos"] == [] and desk["combos"] != []
