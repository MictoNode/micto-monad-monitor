# Pinned to the bookworm variant: the floating python:3.11-slim tag can silently
# move to a newer Debian release (trixie), changing libc/apt behaviour. The
# Python patch version still floats on purpose so security updates arrive
# without a release here.
FROM python:3.11-slim-bookworm

# tzdata: Debian slim has no IANA timezone database; without it glibc cannot
# resolve the container TZ env (e.g. Europe/Istanbul) and silently falls back
# to UTC, so logs and timestamps ignore the operator's .env TZ setting.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Baked in at build time by the release workflow (e.g. v1.4.9)
ARG MONITOR_VERSION=0.0.0
ENV MONITOR_VERSION=${MONITOR_VERSION}

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY monad_monitor/ ./monad_monitor/
COPY scripts/ ./scripts/
COPY config/ ./config/

# Create non-root user for security
# Create state directory with proper ownership before switching user
RUN useradd -m -u 1000 monitor && \
    mkdir -p /app/state && \
    chown -R monitor:monitor /app
USER monitor

# Default config paths
ENV CONFIG_PATH=/app/config/config.yaml
ENV VALIDATORS_PATH=/app/config/validators.yaml

# Run the monitor
RUN chmod +x /app/scripts/entrypoint.sh
CMD ["/app/scripts/entrypoint.sh"]
