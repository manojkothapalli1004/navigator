"""Drift tests: keep the example config files in sync with what Settings reads.

Navigator is configured only through NAVIGATOR_* environment variables. Two files
document them for operators:

* navigator.toml.example: a reference listing every knob with its code default.
  The app does not load it.
* deploy/launchd/com.navigator.plist.example: the launchd template. Its
  EnvironmentVariables dict is what a deployed daemon actually runs with.

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

ROOT = Path(__file__).resolve().parent.parent
TOML_EXAMPLE = ROOT / "navigator.toml.example"
PLIST_EXAMPLE = ROOT / "deploy" / "launchd" / "com.navigator.plist.example"
PACKAGE_DIR = ROOT / "navigator"

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
TOML_NON_SETTINGS_KEYS: set[str] = {"notify_env_file"}


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
