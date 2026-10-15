"""C5j: _safe redacts %40 / '?' / 'scheme:/' values; control chars can't forge lines."""
import pytest

from navigator import cli
from navigator.config import Settings, effective_repos
from tests.test_cli_doctor import _doctor, _git_repo, _plist


@pytest.fixture(autouse=True)
def _sdk_ok(monkeypatch):
    monkeypatch.setattr(cli, "_sdk_version", lambda: "0.2.165")


# (leaky value, secret that must never be printed)
LEAKY_C5J = [
    ("http:/x?token=C5JSECRET1", "C5JSECRET1"),
    ("/path?token=C5JSECRET2", "C5JSECRET2"),
    ("u:C5JSECRET3%40h", "C5JSECRET3"),
]


@pytest.mark.parametrize("value,secret", LEAKY_C5J)
def test_safe_redacts_c5j_inputs(value, secret):
    shown = cli._safe(value)
    assert secret not in shown
    assert shown == "<redacted>"


@pytest.mark.parametrize("value,secret", LEAKY_C5J)
def test_doctor_never_prints_c5j_secret(tmp_path, value, secret):
    repo = _git_repo(tmp_path)
    _, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo),
                                        "NAVIGATOR_WEBHOOK_URL": value}))
    assert secret not in text
    assert "NAVIGATOR_WEBHOOK_URL=<redacted>\n" in text


def test_working_directory_with_userinfo_is_redacted(tmp_path):
    repo = _git_repo(tmp_path)
    plist = _plist(tmp_path, None, top={"Label": "l", "WorkingDirectory": "u:WDSECRET@h",
                                        "EnvironmentVariables": {"NAVIGATOR_REPO": str(repo)}})
    rc, text = _doctor(plist)
    assert rc == 1
    assert "WDSECRET" not in text
    assert "FAIL workingdir: not an absolute path <redacted>" in text


def test_normal_absolute_paths_printed_unchanged(tmp_path):
    repo = _git_repo(tmp_path)
    plist = _plist(tmp_path, None, top={"Label": "l", "WorkingDirectory": str(tmp_path),
                                        "EnvironmentVariables": {"NAVIGATOR_REPO": str(repo)}})
    rc, text = _doctor(plist)
    assert rc == 0, text
    assert f"working directory: {tmp_path}\n" in text
    assert f"ok   repo {repo}\n" in text
    assert cli._safe(str(repo)) == str(repo)


@pytest.mark.parametrize("ch", ["\r", "\n", "\t", "\x00", "\u2028"])
def test_control_chars_map_to_question_mark(ch):
    assert cli._safe(f"/a{ch}b") == "/a?b"
    assert cli._printable(f"K{ch}X") == "K?X"


def test_crlf_maps_to_two_question_marks_directly():
    assert cli._safe("/a\r\nb") == "/a??b"
    assert cli._printable("a\r\nb") == "a??b"


def test_newline_in_value_adds_no_output_line(tmp_path):
    repo = _git_repo(tmp_path)
    _, clean = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo),
                                         "PATH": "/usr/bin FAIL forged"}))
    _, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo),
                                        "PATH": "/usr/bin\r\nFAIL forged"}))
    lines = text.split("\n")
    assert not any(line.startswith("FAIL forged") for line in lines)
    assert len(lines) == len(clean.split("\n"))
    env_line = next(line for line in lines if line.startswith("  PATH="))
    assert "?FAIL forged" in env_line


def test_newline_in_env_key_adds_no_output_line(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    settings = Settings(repo=str(repo))
    monkeypatch.setattr(cli, "_settings_with_env",
                        lambda env, cwd: (settings, effective_repos(settings)))
    _, text = _doctor(_plist(tmp_path, {"NAVIGATOR_REPO": str(repo),
                                        "X\nFAIL forged": "v"}))
    lines = text.split("\n")
    assert not any(line.startswith("FAIL forged") for line in lines)
    assert "  X?FAIL forged=<redacted>" in text
