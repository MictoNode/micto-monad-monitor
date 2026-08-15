"""Generate Prometheus file_sd_configs targets from validators.yaml."""
import json
import os
import sys
from pathlib import Path


def generate_targets(validators_path: str, output_dir: str) -> None:
    """Read validators.yaml and write targets.json for Prometheus."""
    import yaml

    validators_file = Path(validators_path)
    if not validators_file.exists():
        print(f"Validators file not found: {validators_path}")
        sys.exit(1)

    with open(validators_file) as f:
        config = yaml.safe_load(f)

    validators = config.get("validators", [])
    targets = []

    for v in validators:
        if not v.get("enabled", True):
            continue

        name = v["name"]
        host = v["host"]
        metrics_port = v.get("metrics_port", 8889)
        node_exporter_port = v.get("node_exporter_port")
        network = v.get("network", "testnet")

        # Monad node metrics
        targets.append({
            "targets": [f"{host}:{metrics_port}"],
            "labels": {
                "name": name,
                "network": network,
            }
        })

        # Node exporter metrics (optional)
        if node_exporter_port:
            targets.append({
                "targets": [f"{host}:{node_exporter_port}"],
                "labels": {
                    "name": name,
                    "network": network,
                    "job": "node_exporter",
                }
            })

    output_path = Path(output_dir) / "validators.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file in the same directory first, then atomically move it
    # into place with os.replace(). Prometheus file_sd watches validators.json;
    # writing it directly can expose a partially-written file (unexpected end of
    # JSON input). Same directory => same filesystem => os.replace() is atomic.
    temp_path = output_path.with_name(output_path.name + ".tmp")
    with open(temp_path, "w") as f:
        json.dump(targets, f, indent=2)
    os.replace(temp_path, output_path)

    print(f"Generated {len(targets)} targets -> {output_path}")


if __name__ == "__main__":
    vp = os.environ.get("VALIDATORS_PATH", "/app/config/validators.yaml")
    od = os.environ.get("PROMETHEUS_TARGETS_DIR", "/app/state")
    generate_targets(vp, od)
