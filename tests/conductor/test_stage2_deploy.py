"""Stage 2 part 3: serve wiring, deploy plist + installer warning, prompt lines."""
import pathlib
import plistlib

from navigator.conductor import builds as B
from navigator.conductor import runner, server

ROOT = pathlib.Path(B.__file__).resolve().parents[2]


def test_make_builds_off_when_url_unset(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NAVIGATOR_ACT_BASE_URL", raising=False)
    assert server.make_builds(tmp_path) is None
    assert "not configured" in capsys.readouterr().err


def test_make_builds_on_when_url_set(tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_ACT_BASE_URL", "http://127.0.0.1:8090")
    b = server.make_builds(tmp_path)
    assert isinstance(b, B.Builds) and b.path == tmp_path / "builds.json" and b.settings is not None


def test_plist_has_both_env_keys():
    env = plistlib.loads((ROOT / "deploy/launchd/com.calwbot.conductor.plist").read_bytes())["EnvironmentVariables"]
    assert env["NAVIGATOR_ACT_BASE_URL"] == "http://127.0.0.1:8090"
    assert env["NAVIGATOR_REPOS_FILE"] == "/Users/Manoj/calwbot/logs/navigator/repos.json"


def test_installer_warns_without_failing():
    src = (ROOT / "deploy/install_conductor.sh").read_text()
    assert "NAVIGATOR_ACT_BASE_URL" in src and "WARNING" in src
    block = src[src.index("for KEY"):src.index("launchctl bootout")]
    assert "exit" not in block


def test_prompt_mentions_build_delegation():
    p = runner.WINDOWS_PROMPT if hasattr(runner, "WINDOWS_PROMPT") else ""
    assert 'kind="build"' in p and "files" in p and "never merges without the operator" in p
