"""The package version and both plugin manifests carry the same version.

A release bumps three files. Nothing else reads all three, so a missed one
drifts in silence: the command reports one version while a marketplace that
installs this repository as a plugin advertises another.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json")


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    assert isinstance(version, str)
    return version


@pytest.mark.parametrize("relative", MANIFESTS)
def test_manifest_version_matches_pyproject(relative: str) -> None:
    manifest = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    assert manifest["version"] == project_version(), (
        f"{relative} says {manifest['version']}, pyproject.toml says {project_version()}"
    )


@pytest.mark.parametrize("relative", MANIFESTS)
def test_manifest_name_is_the_install_name(relative: str) -> None:
    # A marketplace entry installs by name. When the entry name and the
    # manifest name differ, Claude Code reports the plugin as not found.
    manifest = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    assert manifest["name"] == "enmaas"
