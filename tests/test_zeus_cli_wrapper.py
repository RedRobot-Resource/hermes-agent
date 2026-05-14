import importlib
import os
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_exposes_zeus_console_script():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert pyproject["project"]["scripts"]["zeus"] == "zeus_cli.main:main"


def test_zeus_wrapper_defaults_to_separate_home(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    calls = []

    hermes_main = importlib.import_module("hermes_cli.main")
    monkeypatch.setattr(hermes_main, "main", lambda: calls.append(os.environ["HERMES_HOME"]))

    sys.modules.pop("zeus_cli.main", None)
    zeus_main = importlib.import_module("zeus_cli.main")
    zeus_main.main()

    assert calls == [str(Path.home() / ".zeus")]


def test_zeus_wrapper_preserves_explicit_home(monkeypatch, tmp_path):
    explicit_home = tmp_path / "custom-zeus-home"
    monkeypatch.setenv("HERMES_HOME", str(explicit_home))
    calls = []

    hermes_main = importlib.import_module("hermes_cli.main")
    monkeypatch.setattr(hermes_main, "main", lambda: calls.append(os.environ["HERMES_HOME"]))

    sys.modules.pop("zeus_cli.main", None)
    zeus_main = importlib.import_module("zeus_cli.main")
    zeus_main.main()

    assert calls == [str(explicit_home)]
