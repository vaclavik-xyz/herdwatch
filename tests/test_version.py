import tomllib
from pathlib import Path

import herdwatch


ROOT = Path(__file__).resolve().parents[1]


def test_package_and_plugin_versions_match():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    plugin = tomllib.loads((ROOT / "herdr-plugin.toml").read_text())

    assert project["project"]["version"] == "0.2.1"
    assert plugin["version"] == "0.2.1"
    assert herdwatch.__version__ == "0.2.1"
