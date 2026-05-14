#!/usr/bin/env python3
"""Project Zeus command-line wrapper.

This entry point intentionally delegates to the existing Hermes CLI internals
instead of renaming packages. Its only isolation behavior is to default
HERMES_HOME to ~/.zeus when the caller has not explicitly set one.
"""

from __future__ import annotations

import os
from pathlib import Path


def _ensure_zeus_home() -> None:
    """Keep Zeus state separate from the live Hermes home by default."""

    os.environ.setdefault("HERMES_HOME", str(Path.home() / ".zeus"))


def main() -> None:
    """Run the Hermes CLI through the Zeus-branded entry point."""

    _ensure_zeus_home()

    from hermes_cli.main import main as hermes_main

    hermes_main()


if __name__ == "__main__":
    main()
