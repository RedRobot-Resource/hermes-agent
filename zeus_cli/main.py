#!/usr/bin/env python3
"""Project Zeus command-line wrapper.

This entry point intentionally delegates to the existing Hermes CLI internals
instead of renaming packages. Its isolation behavior is to select the Zeus app
home name before importing Hermes internals, so the shared constants default to
~/.zeus when HERMES_HOME is not explicitly set.
"""

from __future__ import annotations

import os


def _ensure_zeus_home() -> None:
    """Keep Zeus state separate from the live Hermes home by default."""

    os.environ.setdefault("HERMES_APP_HOME_NAME", "zeus")


def main() -> None:
    """Run the Hermes CLI through the Zeus-branded entry point."""

    _ensure_zeus_home()

    from hermes_cli.main import main as hermes_main

    hermes_main()


if __name__ == "__main__":
    main()
