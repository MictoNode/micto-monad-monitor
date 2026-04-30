#!/bin/bash
set -e

# Create empty targets file so Prometheus doesn't error on first read
mkdir -p /app/state
if [ ! -f /app/state/validators.json ]; then
  echo '[]' > /app/state/validators.json
fi

echo "Generating Prometheus targets..."
python /app/scripts/generate_targets.py

echo "Starting monitor..."
exec python -m monad_monitor.main
