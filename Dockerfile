FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY monad_monitor/ ./monad_monitor/
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
CMD ["python", "-m", "monad_monitor.main"]
