"""Tests for scripts/generate_targets.py"""

import importlib.util
import json
from pathlib import Path

import pytest

# scripts/ has no __init__.py, so load the module by file location.
SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "generate_targets.py"


@pytest.fixture(scope="module")
def generate_targets():
    """Load the generate_targets() function from scripts/generate_targets.py"""
    spec = importlib.util.spec_from_file_location("generate_targets", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate_targets


VALIDATORS_YAML = """\
validators:
  - name: validator-1
    host: 10.0.0.1
    enabled: true
    metrics_port: 8889
    node_exporter_port: 9100
"""


def test_generate_targets_writes_atomic_json(tmp_path, generate_targets):
    """Output file is complete JSON, with both targets and no .tmp left behind"""
    validators_path = tmp_path / "validators.yaml"
    validators_path.write_text(VALIDATORS_YAML)

    generate_targets(str(validators_path), str(tmp_path))

    output_path = tmp_path / "validators.json"
    assert output_path.exists()

    targets = json.loads(output_path.read_text())
    assert len(targets) == 2
    addresses = sorted(t["targets"][0] for t in targets)
    assert addresses == ["10.0.0.1:8889", "10.0.0.1:9100"]

    # Temp file must not remain after the call
    assert not (tmp_path / "validators.json.tmp").exists()


def test_generate_targets_without_node_exporter(tmp_path, generate_targets):
    """A validator without node_exporter_port produces a single target"""
    validators_path = tmp_path / "validators.yaml"
    validators_path.write_text(
        """\
validators:
  - name: validator-1
    host: 10.0.0.1
    enabled: true
    metrics_port: 8889
"""
    )

    generate_targets(str(validators_path), str(tmp_path))

    targets = json.loads((tmp_path / "validators.json").read_text())
    assert len(targets) == 1
    assert targets[0]["targets"] == ["10.0.0.1:8889"]
    assert "job" not in targets[0]["labels"]
