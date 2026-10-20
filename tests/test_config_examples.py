"""Drift tests: keep the example config files in sync with what Settings reads.

Navigator is configured only through NAVIGATOR_* environment variables. Two files
document them for operators:

* navigator.toml.example: a reference listing every knob with its code default.
  The app does not load it.
* deploy/launchd/com.navigator.plist.example: the launchd template. Its
  EnvironmentVariables dict is what a deployed daemon actually runs with.
* deploy/launchd/com.calwbot.navigator-telegram.plist.example: the always-on
  Telegram reader's launchd template.
* README.md '## Telegram reader operations': the runbook. Its default paths are
  checked against the DEFAULT_* constants in telegram_inbox.py so they cannot drift.

If a key is misspelled in the plist, launchd silently runs on the code default.
These tests catch that drift.
"""

import plistlib
import re
from dataclasses import MISSING, fields
from pathlib import Path

import pytest

from navigator import config
from navigator.config import Settings
from navigator.observability.telegram_inbox import DEFAULT_INBOX_PATH, DEFAULT_OFFSET_PATH

ROOT = Path(__file__).resolve().parent.parent
TOML_EXAMPLE = ROOT / "navigator.toml.example"
PLIST_EXAMPLE = ROOT / "deploy" / "launchd" / "com.navigator.plist.example"
READER_PLIST_EXAMPLE = ROOT / "deploy" / "launchd" / "com.calwbot.navigator-telegram.plist.example"
README = ROOT / "README.md"
READER_SECTION = "## Telegram reader operations"
PACKAGE_DIR = ROOT / "navigator"

# keys the Telegram reader reads with os.getenv (not Settings)
READER_KEYS: set[str] = {
    "NAVIGATOR_ACT_BASE_URL",
    "NAVIGATOR_TELEGRAM_OFFSET_PATH",
    "NAVIGATOR_TELEGRAM_INBOX_PATH",
    "NAVIGATOR_TELEGRAM_READER_LOCK",
}
# a Telegram bot token looks like 123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")

ENV_KEY_RE = re.compile(r"\bNAVIGATOR_[A-Z0-9]+(?:_[A-Z0-9]+)*\b")

# Keys that Settings reads but that are intentionally left out of both example files.
UNDOCUMENTED_ALLOWLIST: set[str] = set()

# Secret values. Settings must not require them to appear in an example file.
SECRET_KEYS: set[str] = {
    "NAVIGATOR_TELEGRAM_BOT_TOKEN",
    "NAVIGATOR_TELEGRAM_CHAT_ID",
}

# Documented keys that are read outside Settings, by the notifier.
# They must still be referenced somewhere in the navigator package.
READ_OUTSIDE_SETTINGS: set[str] = {
    "NAVIGATOR_TELEGRAM_BOT_TOKEN",
    "NAVIGATOR_TELEGRAM_CHAT_ID",
    "NAVIGATOR_NOTIFY_ENV_FILE",
}

# navigator.toml.example keys that are not Settings fields.
TOML_NON_SETTINGS_KEYS: set[str] = {"notify_env_file", "repos_file"}


def _settings_env_keys(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Run Settings.from_env() with a recording getenv. Return every name it reads."""
    seen: set[str] = set()

    def recording_getenv(name, default=None):
        seen.add(name)
        return default

    monkeypatch.setattr(config.os, "getenv", recording_getenv)
    Settings.from_env()
    return seen


@pytest.fixture
def settings_keys(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    keys = _settings_env_keys(monkeypatch)
    assert keys, "introspection found no env vars read by Settings.from_env()"
    assert all(k.startswith("NAVIGATOR_") for k in keys), keys
    return keys


def _toml_documented_keys() -> set[str]:
    return set(ENV_KEY_RE.findall(TOML_EXAMPLE.read_text()))


def _plist_env() -> dict:
    with PLIST_EXAMPLE.open("rb") as fh:
        plist = plistlib.load(fh)
    env = plist.get("EnvironmentVariables")
    assert isinstance(env, dict), "plist example has no EnvironmentVariables dict"
    return env


def _plist_documented_keys() -> set[str]:
    return {k for k in _plist_env() if k.startswith("NAVIGATOR_")}


def _package_source() -> str:
    return "\n".join(p.read_text() for p in sorted(PACKAGE_DIR.rglob("*.py")))


def test_settings_introspection_sees_role_model_overrides(settings_keys):
    for role in ("PLANNER", "BUILDER", "VERIFIER"):
        assert f"NAVIGATOR_SDK_MODEL_{role}" in settings_keys


def test_toml_example_documents_only_known_keys(settings_keys):
    known = settings_keys | READ_OUTSIDE_SETTINGS
    unknown = _toml_documented_keys() - known
    assert not unknown, f"navigator.toml.example documents keys nothing reads: {sorted(unknown)}"


def test_plist_example_sets_only_known_keys(settings_keys):
    known = settings_keys | READ_OUTSIDE_SETTINGS
    unknown = _plist_documented_keys() - known
    assert not unknown, (
        f"launchd plist example sets keys nothing reads (typo?): {sorted(unknown)}"
    )


def test_every_settings_key_is_documented(settings_keys):
    documented = _toml_documented_keys() | _plist_documented_keys()
    required = settings_keys - SECRET_KEYS - UNDOCUMENTED_ALLOWLIST
    missing = required - documented
    assert not missing, (
        f"Settings reads keys that appear in no example file: {sorted(missing)}. "
        "Document them in navigator.toml.example or add them to UNDOCUMENTED_ALLOWLIST."
    )


def test_allowlist_has_no_stale_entries(settings_keys):
    stale = UNDOCUMENTED_ALLOWLIST - settings_keys
    assert not stale, f"UNDOCUMENTED_ALLOWLIST names keys Settings no longer reads: {sorted(stale)}"


def test_keys_read_outside_settings_are_still_referenced():
    source = _package_source()
    unreferenced = {k for k in READ_OUTSIDE_SETTINGS if k not in source}
    assert not unreferenced, (
        f"READ_OUTSIDE_SETTINGS lists keys no navigator module references: {sorted(unreferenced)}"
    )


def test_plist_example_defaults_to_dry_run():
    assert _plist_env().get("NAVIGATOR_MODE") == "dry_run"


# ---- Telegram reader plist example ------------------------------------------

def _reader_plist() -> dict:
    with READER_PLIST_EXAMPLE.open("rb") as fh:
        plist = plistlib.load(fh)
    assert isinstance(plist, dict)
    return plist


def _reader_env() -> dict:
    env = _reader_plist().get("EnvironmentVariables")
    assert isinstance(env, dict), "reader plist example has no EnvironmentVariables dict"
    return env


def test_reader_plist_label():
    assert _reader_plist()["Label"] == "com.calwbot.navigator-telegram"


def test_reader_plist_runs_the_telegram_reader():
    args = _reader_plist()["ProgramArguments"]
    assert isinstance(args, list) and len(args) >= 4
    assert args[-3:] == ["-m", "navigator", "telegram-reader"]
    assert Path(args[0]).is_absolute(), args[0]
    assert args[0].endswith("/python"), args[0]


def test_reader_plist_keeps_alive():
    assert _reader_plist().get("KeepAlive") is True


def test_reader_plist_working_directory_is_absolute():
    wd = _reader_plist().get("WorkingDirectory")
    assert isinstance(wd, str) and Path(wd).is_absolute(), wd


def test_reader_plist_log_paths():
    plist = _reader_plist()
    out, err = plist.get("StandardOutPath"), plist.get("StandardErrorPath")
    assert isinstance(out, str) and out.endswith("telegram-reader.out.log"), out
    assert isinstance(err, str) and err.endswith("telegram-reader.err.log"), err
    assert Path(out).is_absolute() and Path(err).is_absolute()
    assert out != err


def test_reader_plist_never_makes_the_daemon_poll():
    # one getUpdates consumer per bot: the reader refuses to start when this is "1"
    env = _reader_env()
    assert "NAVIGATOR_TELEGRAM_COMMANDS" not in env, env


def test_reader_plist_sets_only_known_keys(settings_keys):
    known = settings_keys | READ_OUTSIDE_SETTINGS | READER_KEYS
    unknown = {k for k in _reader_env() if k.startswith("NAVIGATOR_")} - known
    assert not unknown, f"reader plist example sets keys nothing reads (typo?): {sorted(unknown)}"


def test_reader_keys_are_referenced_in_package():
    source = _package_source()
    unreferenced = {k for k in READER_KEYS if k not in source}
    assert not unreferenced, sorted(unreferenced)


def test_reader_plist_uses_env_file_and_act_url():
    env = _reader_env()
    assert Path(env["NAVIGATOR_NOTIFY_ENV_FILE"]).is_absolute()
    assert env["NAVIGATOR_ACT_BASE_URL"] == "http://127.0.0.1:8090"


def test_reader_plist_has_no_secret_looking_values():
    env = _reader_env()
    assert "NAVIGATOR_TELEGRAM_BOT_TOKEN" not in env
    assert "NAVIGATOR_TELEGRAM_CHAT_ID" not in env
    for key, value in env.items():
        assert not TOKEN_RE.search(str(value)), key
        assert "YOU" in str(value) or not str(value).startswith("/Users/"), (
            f"{key}: real home directory in an example; use /Users/YOU")
    raw = READER_PLIST_EXAMPLE.read_text()
    assert not TOKEN_RE.search(raw), "bot token in the reader plist example"


def test_reader_plist_override_block_is_commented_out_and_explains_sharing():
    raw = READER_PLIST_EXAMPLE.read_text()
    env = _reader_env()
    for key in ("NAVIGATOR_TELEGRAM_OFFSET_PATH", "NAVIGATOR_TELEGRAM_INBOX_PATH",
                "NAVIGATOR_TELEGRAM_READER_LOCK"):
        assert key in raw, f"{key} override example missing"
        assert key not in env, f"{key} must stay commented out in the example"
    assert "DEFAULT_*" in raw
    assert "navigator/observability/telegram_inbox.py" in raw
    assert "set the SAME values in the daemon plist" in raw
    assert "navigator inbox" in raw
    assert "relative" not in raw.lower()


# ---- README runbook ------------------------------------------------------------

def _readme_reader_section() -> str:
    text = README.read_text()
    assert READER_SECTION in text, f"README.md lacks the {READER_SECTION!r} section"
    start = text.index(READER_SECTION)
    rest = text[start + len(READER_SECTION):]
    m = re.search(r"\n## ", rest)
    return rest if m is None else rest[:m.start()]


def test_readme_reader_section_states_the_code_defaults():
    section = _readme_reader_section()
    assert DEFAULT_OFFSET_PATH in section
    assert DEFAULT_INBOX_PATH in section
    assert ".reader.lock" in section
    assert "DEFAULT_OFFSET_PATH" in section and "DEFAULT_INBOX_PATH" in section
    assert "telegram_inbox.py" in section


def test_readme_reader_section_warns_that_three_consumers_share_paths():
    section = _readme_reader_section()
    assert "navigator inbox" in section
    assert "NAVIGATOR_TELEGRAM_OFFSET_PATH" in section
    assert "NAVIGATOR_TELEGRAM_INBOX_PATH" in section
    assert "NAVIGATOR_TELEGRAM_READER_LOCK" in section
    assert "all three together or none" in section


def test_readme_reader_section_does_not_call_defaults_relative():
    section = _readme_reader_section().lower()
    for m in re.finditer(r"relative", section):
        window = section[max(0, m.start() - 200):m.end() + 200]
        assert "default" not in window, (
            "README calls a default path relative; the defaults are absolute constants")


def test_readme_reader_section_says_doctor_checks_only_the_daemon_plist():
    section = _readme_reader_section().replace("\n", " ")
    sentences = re.split(r"[.!?](?:\s|$)", section)
    assert any("doctor" in s and "daemon" in s for s in sentences), (
        "no sentence mentions both doctor and daemon")


def test_readme_reader_section_covers_the_runbook():
    section = _readme_reader_section()
    assert "com.calwbot.navigator-telegram" in section
    assert "com.calwbot.navigator`" in section
    assert "NAVIGATOR_TELEGRAM_COMMANDS" in section
    assert "409" in section and "30 s" in section and "per hour per error kind" in section
    assert "exit 2" in section
    assert "telegram-reader.out.log" in section and "telegram-reader.err.log" in section
    assert "launchctl kickstart -k" in section
    assert "bootout" in section and "bootstrap" in section
    assert "http://127.0.0.1:8090" in section
    assert not TOKEN_RE.search(section)


def test_toml_example_defaults_match_settings():
    tomllib = pytest.importorskip("tomllib")
    data = tomllib.loads(TOML_EXAMPLE.read_text())
    flat: dict = {}
    for section, values in data.items():
        assert isinstance(values, dict), f"top-level key {section!r} is not a section"
        for key, value in values.items():
            assert key not in flat, f"duplicate key {key!r} in navigator.toml.example"
            flat[key] = value

    defaults = {}
    for f in fields(Settings):
        if f.default is not MISSING:
            defaults[f.name] = f.default
        elif f.default_factory is not MISSING:  # type: ignore[misc]
            defaults[f.name] = f.default_factory()  # type: ignore[misc]

    mismatches = []
    for key, value in flat.items():
        if key in TOML_NON_SETTINGS_KEYS:
            continue
        assert key in defaults, f"navigator.toml.example key {key!r} is not a Settings field"
        documented = None if value == "" else value
        if documented != defaults[key]:
            mismatches.append((key, value, defaults[key]))
    assert not mismatches, f"documented defaults differ from code (key, doc, code): {mismatches}"
