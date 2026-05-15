import importlib
import os
import sys
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean_zeus_app_home_name(monkeypatch):
    monkeypatch.delenv("HERMES_APP_HOME_NAME", raising=False)
    yield
    os.environ.pop("HERMES_APP_HOME_NAME", None)


def test_pyproject_exposes_zeus_console_script():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert pyproject["project"]["scripts"]["zeus"] == "zeus_cli.main:main"


def test_zeus_wrapper_selects_zeus_app_home_without_forcing_hermes_home(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_APP_HOME_NAME", raising=False)
    calls = []

    hermes_main = importlib.import_module("hermes_cli.main")
    monkeypatch.setattr(
        hermes_main,
        "main",
        lambda: calls.append(
            (os.environ.get("HERMES_APP_HOME_NAME"), os.environ.get("HERMES_HOME"))
        ),
    )

    sys.modules.pop("zeus_cli.main", None)
    zeus_main = importlib.import_module("zeus_cli.main")
    zeus_main.main()

    assert calls == [("zeus", None)]


def test_zeus_wrapper_preserves_explicit_home(monkeypatch, tmp_path):
    explicit_home = tmp_path / "custom-zeus-home"
    monkeypatch.setenv("HERMES_HOME", str(explicit_home))
    monkeypatch.delenv("HERMES_APP_HOME_NAME", raising=False)
    calls = []

    hermes_main = importlib.import_module("hermes_cli.main")
    monkeypatch.setattr(
        hermes_main,
        "main",
        lambda: calls.append(
            (os.environ.get("HERMES_APP_HOME_NAME"), os.environ["HERMES_HOME"])
        ),
    )

    sys.modules.pop("zeus_cli.main", None)
    zeus_main = importlib.import_module("zeus_cli.main")
    zeus_main.main()

    assert calls == [("zeus", str(explicit_home))]


def test_zeus_top_level_help_uses_zeus_command_name(monkeypatch):
    """Zeus help should not expose the Hermes executable name in usage."""
    monkeypatch.setenv("HERMES_APP_HOME_NAME", "zeus")
    parser_module = importlib.import_module("hermes_cli._parser")

    parser, _, _ = parser_module.build_top_level_parser()
    help_text = parser.format_help()

    assert help_text.startswith("usage: zeus")
    assert "zeus chat -q \"Hello\"" in help_text
    assert "usage: hermes" not in help_text


def test_hermes_top_level_help_keeps_hermes_command_name(monkeypatch):
    """Normal Hermes CLI help remains unchanged when no Zeus app name is set."""
    monkeypatch.delenv("HERMES_APP_HOME_NAME", raising=False)
    parser_module = importlib.import_module("hermes_cli._parser")

    parser, _, _ = parser_module.build_top_level_parser()
    help_text = parser.format_help()

    assert help_text.startswith("usage: hermes")
    assert "hermes chat -q \"Hello\"" in help_text
