#!/usr/bin/env python3
"""Run the bundled output policy without installed packages or site imports."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_MAX_INPUT_BYTES = 1024 * 1024


def main() -> None:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from enmaas_cli.containment import check_tool

        raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            raise ValueError("hook input too large")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook input must be an object")
        reason = check_tool(event)
    except Exception as exc:
        reason = (
            "enmaas containment: hook could not check tool input "
            f"({type(exc).__name__}). Check the plugin installation and event format."
        )
    if reason is not None:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            sys.stdout,
        )
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
