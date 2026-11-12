#!/usr/bin/env python3
"""Run the bundled settings helper without importing the CLI package."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> None:
    helper = Path(__file__).resolve().parents[1] / "src" / "enmaas_cli" / "hostconfig.py"
    runpy.run_path(str(helper))["main"](sys.argv)


if __name__ == "__main__":
    main()
