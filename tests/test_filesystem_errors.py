"""Tests for filesystem error handling."""

import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from enmaas_cli.cli import main


def test_permission_error_exits_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PermissionError should exit 1 with an error message, not a traceback."""
    # Set up environment
    monkeypatch.setenv("ENMAAS_API_KEY", "sk-test-key")

    # Create a config file
    config = tmp_path / "config.toml"
    config.write_text(
        """
base_url = "https://mock.example.com"
default_model = "rits/zai-org/glm-5-3"
""",
        encoding="utf-8",
    )

    # Capture stderr
    stderr_capture = StringIO()
    monkeypatch.setattr(sys, "stderr", stderr_capture)

    # Simulate a permission error when creating the output directory
    output_path = str(tmp_path / "ro" / "out.md")
    with patch("pathlib.Path.mkdir", side_effect=PermissionError("Permission denied")):
        exit_code = main(["--config", str(config), "ask", "-p", "hi", "-o", output_path])

    assert exit_code == 1
    stderr_output = stderr_capture.getvalue()
    assert "error:" in stderr_output
    assert "Permission denied" in stderr_output
    # Should not contain Python traceback markers
    assert "Traceback" not in stderr_output
