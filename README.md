# Monad Validator Monitor

[![Version](https://img.shields.io/badge/version-1.4.0-8B5CF6?style=flat-square)](https://github.com/MictoNode/micto-monad-monitor)
[![Python](https://img.shields.io/badge/python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?style=flat-square&logo=docker&logoColor=white)](https://docker.com)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)

> Monitor your Monad validators from a separate server. Get instant alerts when your node goes down, stops producing blocks, or runs low on resources.

**Why remote monitoring?** If your validator crashes, local monitoring dies with it. This runs elsewhere, so you always get alerts.

---

## What's New

- **Metrics Dashboard** - 27 Prometheus charts across 7 sections at `http://your-server:8383`
- **Monitor Dashboard** - Real-time validator status at `http://your-server:8282`
- **Time range selector** - 1m, 5m, 30m, 1h, All per chart section
- **Multi-source validation** - Huginn + gmonads API cross-validation
- **Active set tracking** - Know when your validator enters/leaves active set
- **Pushover emergency alerts** - Bypass Do Not Disturb mode
- **Discord webhook support** - Community alerts
- **Slack webhook support** - Team alerts
- **All alert channels optional** - Use any combination of Telegram, Pushover, Discord, Slack

---

## Prerequisites

### Docker Installation

If Docker is not installed on your server (both monitor and validator servers need Docker):

```bash
# Install Docker (Ubuntu/Debian)
curl -fsSL https://get.docker.com | sh

# Add your user to docker group (optional, avoids sudo)
sudo usermod -aG docker $USER

# Log out and back in, then verify:
docker --version
docker compose version
```

**Requirements:**
- Docker 20.10+
- Docker Compose v2 (`docker compose` command)
- 512MB RAM minimum (monitor server)
- Internet access to reach your validators

> **Note:** This guide uses Docker Compose v2 commands (`docker compose`). If you're using v1, replace with `docker-compose` (hyphen).

---

## Quick Start (5 minutes)

```bash
# 1. Clone & enter
git clone https://github.com/MictoNode/micto-monad-monitor.git
cd micto-monad-monitor

# 2. Copy example configs
cp config/config.example.yaml config/config.yaml
cp config/validators.example.yaml config/validators.yaml
cp .env.example .env

# 3. Edit configs
nano .env
nano config/validators.yaml
nano config/config.yaml

# 4. Start (uses pre-built image from GHCR)
docker compose up -d
docker compose logs -f
```

> **Want to build from source?** Uncomment `build: .` and comment out the `image:` line in `docker-compose.yaml`, then run `docker compose up -d --build`.

You should get a **"Monad Monitor Started"** message on your configured alert channel(s).

**Monitor Dashboard:** `http://your-server-ip:8282` — Real-time validator status
**Metrics Dashboard:** `http://your-server-ip:8383` — Prometheus charts (requires `DASHBOARD_PASSWORD` + `DASHBOARD_JWT_SECRET` in `.env`)

---

## What You Get

| Alert Type | When | Channels |
|------------|------|----------|
| **Node Down** | Can't reach metrics or blocks stopped | Telegram + Pushover + Discord + Slack |
| **High Resources (Critical)** | CPU/RAM/Disk ≥ 95% | Telegram + Pushover + Discord + Slack |
| **High Resources (Warning)** | CPU/RAM/Disk ≥ 90% | Telegram + Discord + Slack |
| **Active Set Changes** | Enters or leaves active set | Telegram + Discord + Slack |
| **Recovery** | Validator back online | Telegram + Discord + Slack |
| **Extended Report** | 6-hour detailed report with uptime | Telegram + Discord + Slack |

**Alert Priority:**
- **CRITICAL** → Telegram + Pushover + Discord + Slack (bypasses rate limits)
- **WARNING** → Telegram + Discord + Slack (rate limited)
- **INFO** → Telegram + Discord + Slack (rate limited)

> **Notes:**
> - All channels are optional — configure any combination
> - Pushover: Only CRITICAL alerts (emergency channel), 30-minute cooldown per validator
> - Discord/Slack: Optional, receives ALL alerts if configured

---

## Setup Guide

### What You Need

- **2 servers:** One for your validator, one for monitoring (can be a cheap VPS)
- **Telegram bot** (free, takes 2 minutes)
- **Discord webhook** (optional, free, takes 2 minutes)
- **Slack webhook** (optional, free, takes 2 minutes)
- **Pushover** (optional but recommended, for emergency alerts that bypass DND)

---

### Step 1: Prepare Your Validator Server

Open these ports to your monitor server IP only:

```bash
MONITOR_IP="1.2.3.4"  # <-- Your monitor server IP

sudo ufw allow from $MONITOR_IP to any port 8889 proto tcp  # Prometheus metrics
sudo ufw allow from $MONITOR_IP to any port 8080 proto tcp  # JSON-RPC
sudo ufw allow from $MONITOR_IP to any port 9100 proto tcp  # Node exporter (optional)
```

#### Optional: System Metrics (CPU/RAM/Disk + TrieDB)

**1. Install TrieDB Collector** (for MonadDB disk usage):

```bash
# Install bc calculator
sudo apt install -y bc

# Create directories
mkdir -p ~/monad-monitoring/scripts
sudo mkdir -p /var/lib/node_exporter/textfile_collector

# Get the collector script
curl -o ~/monad-monitoring/scripts/triedb-collector.sh \
  https://raw.githubusercontent.com/MictoNode/micto-monad-monitor/main/scripts/triedb-collector.sh

chmod +x ~/monad-monitoring/scripts/triedb-collector.sh

# Test - creates .prom file
~/monad-monitoring/scripts/triedb-collector.sh

# Verify
cat /var/lib/node_exporter/textfile_collector/monad_triedb.prom

# Add to crontab (runs every minute)
crontab -e
# Add this line:
* * * * * $HOME/monad-monitoring/scripts/triedb-collector.sh >> /var/log/triedb-collector.log 2>&1
```

**2. Install Node Exporter** (with textfile collector for TrieDB):

```bash
docker run -d \
  --name node-exporter \
  --restart unless-stopped \
  --network=host \
  -v /proc:/host/proc:ro \
  -v /sys:/host/sys:ro \
  -v /:/rootfs:ro \
  -v /var/lib/node_exporter/textfile_collector:/textfile_collector \
  prom/node-exporter:latest \
  --path.procfs=/host/proc \
  --path.sysfs=/host/sys \
  --path.rootfs=/rootfs \
  --web.listen-address=:9100 \
  --collector.textfile.directory=/textfile_collector

# Verify (should see both system metrics AND monad_triedb_* metrics)
curl http://localhost:9100/metrics | grep monad_triedb

> **Note:** To use a different port, change `--web.listen-address=:9100` to your desired port (e.g. `:9200`).
> Don't forget to also update `node_exporter_port` in `validators.yaml` to match.
```

> **Note:** `--network=host` is required for correct network interface names.
> Without it, node_exporter reports the container's virtual `eth0` instead of
> real host interfaces (e.g. `enp5s0`). Port mapping (`-p`) is not needed in
> host mode — the container listens directly on the host's port 9100.

---

### Step 2: Create Telegram Bot

1. Open Telegram, search **@BotFather**
2. Send `/newbot` and follow prompts
3. Save the **token** (looks like `123456789:ABCdef...`)
4. Start a chat with your bot, send any message
5. Get your **chat_id**: Open `https://api.telegram.org/bot<TOKEN>/getUpdates`
6. Find `"chat":{"id":123456789}` - that's your chat_id

**Test:**
```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -d "chat_id=<CHAT_ID>&text=Test"
```

---

### Step 3: Pushover (Optional - Recommended)

For alerts that bypass your phone's Do Not Disturb:

1. Go to [pushover.net](https://pushover.net), create account
2. Note your **User Key**
3. Create an app, get **API Token**
4. Install Pushover app on your phone

> $4.99 one-time per device after 30-day trial. Desktop notifications are free.

---

### Step 4: Configure the Monitor

```bash
# 1. Clone & enter
git clone https://github.com/MictoNode/micto-monad-monitor.git
cd micto-monad-monitor

# 2. Copy example configs
cp config/config.example.yaml config/config.yaml
cp config/validators.example.yaml config/validators.yaml
cp .env.example .env
```

> **Want to build from source?** Uncomment `build: .` and comment out the `image:` line in `docker-compose.yaml`, then run `docker compose up -d --build`.

#### 4.1 Edit `.env` - Your Credentials

```bash
nano .env
```

| Variable | Required | Description |
|----------|:--------:|-------------|
| `TELEGRAM_TOKEN` | No | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | No | Your chat ID for alerts |
| `PUSHOVER_USER_KEY` | No | For emergency alerts |
| `PUSHOVER_APP_TOKEN` | No | From pushover.net |
| `DISCORD_WEBHOOK_URL` | No | Discord webhook URL |
| `SLACK_WEBHOOK_URL` | No | Slack incoming webhook URL |
| `DASHBOARD_PASSWORD` | No | Metrics dashboard password (empty = disabled) |
| `DASHBOARD_JWT_SECRET` | No | JWT secret for metrics dashboard (`openssl rand -hex 32`) |
| `TZ` | No | Timezone (default: UTC) |

> At least one alert channel must be configured.
> Metrics Dashboard requires both `DASHBOARD_PASSWORD` and `DASHBOARD_JWT_SECRET` to be set.

Save: `Ctrl+O`, Exit: `Ctrl+X`

#### 4.2 Edit `validators.yaml` - Your Validators

```bash
nano config/validators.yaml
```

```yaml
validators:
  - name: "My Validator"
    host: "192.168.1.100"
    network: "testnet"
    metrics_port: 8889
    rpc_port: 8080
    node_exporter_port: 9100    # Optional - delete if not using
    validator_secp: "02abc123..."  # 66 chars, starts with 02/03
    enabled: true
```

| Field | Required | Description |
|-------|:--------:|-------------|
| `name` | **Yes** | Display name |
| `host` | **Yes** | Validator IP |
| `network` | **Yes** | `testnet` or `mainnet` |
| `metrics_port` | **Yes** | Default: 8889 |
| `rpc_port` | **Yes** | Default: 8080 |
| `node_exporter_port` | No | Delete if not using system metrics |
| `validator_secp` | **Yes** | 66 chars, starts with 02/03 |
| `enabled` | No | Set to `false` to disable (default: true) |

Save: `Ctrl+O`, Exit: `Ctrl+X`

#### 4.3 Edit `config.yaml` - Settings (Optional)

```bash
nano config/config.yaml
```

Default settings work for most users. Key options:

```yaml
monitoring:
  check_interval: 60           # Seconds between checks
  alert_threshold: 3           # Failures before alerting
  extended_report_interval: 21600  # 6-hour detailed report

thresholds:
  cpu_warning: 90
  cpu_critical: 95
  memory_warning: 90
  memory_critical: 95
  disk_warning: 85
  disk_critical: 95
```

Save: `Ctrl+O`, Exit: `Ctrl+X`

---

### Step 5: Start Monitoring

```bash
docker compose up -d
docker compose logs -f
```

You should see:
```
INFO ✅ My Monad Testnet: In-sync · Height: 15,079,199 · Peers: 204
```

**Done!** Check your Telegram for the startup message.

![Telegram Preview](assets/telegram.png)

---

## Monitor Dashboard

Real-time validator status overview at: `http://your-server-ip:8282`

![Dashboard Preview](assets/dashboard.png)

### Features

Each validator card displays:

| Metric | Description |
|--------|-------------|
| **Status** | ACTIVE / WARNING / INACTIVE / CRITICAL |
| **Height** | Current block height |
| **Peers** | Connected peer count |
| **Uptime** | Huginn uptime percentage |
| **Fails** | Consecutive check failures |

- **5-second auto-refresh** - Real-time updates
- **Status legend** - Color-coded indicators
- **Connection status** - Monitor connectivity badge
- **Responsive design** - Works on mobile, tablet, desktop

### Connect Your Domain (Optional)

1. **Point your domain** (e.g., `monad-monitor.yourdomain.com`) to your server IP

2. **Create nginx config:**
   ```bash
   sudo nano /etc/nginx/sites-available/monad-monitor
   ```

   ```nginx
   server {
       server_name monad-monitor.yourdomain.com;

       location / {
           proxy_pass http://localhost:8282;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       }

       listen 80;
   }
   ```

3. **Enable & test:**
   ```bash
   sudo ln -s /etc/nginx/sites-available/monad-monitor /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   ```

4. **Add SSL (recommended):**
   ```bash
   sudo certbot --nginx -d monad-monitor.yourdomain.com
   ```

---

## Metrics Dashboard

Production-grade metrics dashboard with Prometheus time-series charts at `http://your-server-ip:8383`.

![Metrics Dashboard Preview](assets/dashboard-metrics.png)

### Setup

1. Add to your `.env` file (see Step 4.1):
   ```env
   DASHBOARD_PASSWORD=your_secure_password
   DASHBOARD_JWT_SECRET=<generate with: openssl rand -hex 32>
   ```

2. Restart services:
   ```bash
   docker compose up -d
   ```

3. Open `http://your-server:8383` and enter your password.

> Prometheus starts automatically with `docker compose up` and scrapes validator metrics from `:8889` and `:9100`.

### Overview

After login, the dashboard shows **6 stat boxes** and **27 time-series charts** across **7 collapsible sections**:

| Stat Box | Description |
|----------|-------------|
| **Node Status** | UP / DOWN with checkmark indicator |
| **Block Height** | Current block height |
| **Sync Status** | In-sync / behind percentage |
| **Self Stake** | Your validator's stake percentage |
| **Total Peers** | Connected peer count |
| **Uptime** | Validator uptime percentage |

### Chart Sections

| Section | Charts | What You See |
|---------|:------:|--------------|
| **Consensus & Execution** | 7 | Block height, time, commit rate, proposals, TC ratio, leader changes |
| **Peer & Network** | 3 | Connected peers, network I/O per interface |
| **Raptorcast** | 4 | Decoding rate, cache hit ratio, queue depth, insertions |
| **Txpool** | 3 | Pending/queued transactions, gas pricing |
| **RPC** | 5 | Active requests, execution duration, call rate per method, wait time, per-method latency |
| **Host** | 8 | CPU, memory, load, disk I/O, filesystem usage, NVMe temperature & wear level |
| **TrieDB** | 1 | Fast/slow/free tier distribution |

### Features

- **Per-section time range selector** — 1m, 5m, 30m, 1h, All (independent per section)
- **Tab-based validator selection** — One tab per configured validator
- **Threshold lines** — Visual markers on block time, disk usage, NVMe temp & wear
- **Multi-device support** — NVMe chips, disk devices, mountpoints, network interfaces shown separately
- **30-second auto-refresh** — With countdown, pauses when tab is hidden
- **JWT authentication** — httpOnly cookie, 24-hour expiry, password-protected
- **Purple dark theme** — Consistent design across both dashboards
- **Unit-formatted tooltips** — ms, %, bytes, ops/s displayed correctly
- **Responsive design** — Works on mobile, tablet, desktop

### Connect Your Domain (Optional)

1. **Point your domain** (e.g., `monad-metrics.yourdomain.com`) to your server IP

2. **Create nginx config:**
   ```bash
   sudo nano /etc/nginx/sites-available/monad-metrics
   ```

   ```nginx
   server {
       server_name monad-metrics.yourdomain.com;

       location / {
           proxy_pass http://localhost:8383;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       }

       listen 80;
   }
   ```

3. **Enable & test:**
   ```bash
   sudo ln -s /etc/nginx/sites-available/monad-metrics /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   ```

4. **Add SSL (recommended):**
   ```bash
   sudo certbot --nginx -d monad-metrics.yourdomain.com
   ```

### Disable

Leave `DASHBOARD_PASSWORD` and `DASHBOARD_JWT_SECRET` empty (or remove them) to disable the metrics dashboard. All other services continue normally.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│              MONITORING SERVER                   │
│                                                  │
│  Docker Compose                                 │
│  ├── Monitor Container                          │
│  │   ├── Monitor (checks validators)            │
│  │   ├── Health Server :8181 (internal)         │
│  │   ├── Monitor Dashboard :8282                │
│  │   └── Metrics Dashboard :8383 (FastAPI)      │
│  │                                               │
│  └── Prometheus Container :9090 (30d retention) │
│                                                  │
│  Nginx (optional)                               │
│  ├── monad-monitor.domain.com → :8282           │
│  └── monad-metrics.domain.com  → :8383          │
└────────────────────┬────────────────────────────┘
                     │
       ┌─────────────┼─────────────┐
       ▼             ▼             ▼
┌──────────┐  ┌──────────┐  ┌──────────┐
│Validator │  │Validator │  │Validator │
│ :8889    │  │ :8889    │  │ :8889    │
│ :8080    │  │ :8080    │  │ :8080    │
│ :9100    │  │ :9100    │  │ :9100    │
└──────────┘  └──────────┘  └──────────┘
```

---

## Troubleshooting

### "Connection failed"

```bash
# Test from monitor server:
curl http://VALIDATOR_IP:8889/metrics

# If timeout:
# 1. Check firewall on validator
# 2. Verify IP in validators.yaml
# 3. Ensure Monad node is running
```

### No Telegram messages

```bash
# Test manually:
curl -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -d "chat_id=<CHAT_ID>&text=Test"
```

### Dashboard not loading

```bash
# Check container logs:
docker compose logs | grep -E "8282|8383"

# Verify containers are running:
docker compose ps

# Metrics dashboard not working? Check env vars:
docker compose exec monitor env | grep DASHBOARD
```

### Too many alerts

```yaml
# In config.yaml:
monitoring:
  alert_threshold: 5  # More failures before alerting
```

### State not persisting (false alerts on restart)

```bash
# Verify volume is mounted:
docker volume ls | grep monitor-state

# Check volume contents:
docker run --rm -v monitor-state:/data alpine ls -la /data
```

---

## Files

```
micto-monad-monitor/
├── .env                        # Your secrets (Telegram, Pushover, etc.)
├── docker-compose.yaml         # Docker config
├── config/
│   ├── config.yaml            # Settings (thresholds, intervals)
│   └── validators.yaml        # Your validators
├── scripts/
│   └── triedb-collector.sh    # TrieDB + NVMe metrics (run on validator)
└── monad_monitor/
    ├── main.py                # Entry point
    ├── alerts.py              # Telegram, Pushover, Discord, Slack
    ├── dashboard_server.py    # Monitor dashboard (:8282)
    ├── api_server.py          # Metrics dashboard API (:8383)
    ├── health_server.py       # Health API (:8181)
    ├── static/                # Monitor dashboard frontend
    └── static_dashboard/      # Metrics dashboard frontend (Chart.js)
```

---

## API Endpoints

### Health Server (:8181)

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Full health status (JSON) |
| `GET /ready` | Readiness probe |
| `GET /live` | Liveness probe |
| `GET /metrics` | Prometheus metrics |

### Monitor Dashboard (:8282)

| Endpoint | Description |
|----------|-------------|
| `GET /` | Web dashboard UI |
| `GET /health` | Health status (JSON) |

### Metrics Dashboard (:8383)

| Endpoint | Description |
|----------|-------------|
| `POST /api/auth/login` | Login with password, returns JWT cookie |
| `POST /api/auth/logout` | Clear JWT cookie |
| `GET /api/health` | Prometheus connectivity check |
| `GET /api/validators` | List configured validators |
| `GET /api/overview/{name}` | Stat box data for a validator |
| `GET /api/metrics/{name}` | Raw metric values for a validator |
| `GET /api/chart/{name}/{key}?range=1h` | Time-series chart data (ranges: 1m, 5m, 30m, 1h, All) |

---

## External APIs

| API | Purpose | Rate Limit |
|-----|---------|------------|
| [Huginn Tech](https://huginn.tech) | Validator uptime, active set | 5 validators/hour |
| [gmonads.com](https://gmonads.com) | Network TPS, block fullness, fallback | 30 req/min |

---

## Credits

| Source | Purpose |
|--------|---------|
| [Huginn Tech](https://huginn.tech) | Validator uptime, active set detection |
| [gmonads.com](https://gmonads.com) | Network TPS, block fullness, fallback |
| [Staking4all](https://github.com/staking4all/monad-monitoring) | TrieDB collector reference |

---

## Updating

### General Update Steps

Every update follows the same pattern:

```bash
# 1. Pull the latest version
docker compose pull          # Pre-built image (GHCR)
# — OR —
git pull                     # Build from source

# 2. Stop and restart
docker compose up -d

# 3. Verify
docker compose logs -f
```

> State files in `/app/state` are preserved across updates via Docker volume. No backup needed.

### Pre-built vs Source

| Method | Command | Use When |
|--------|---------|----------|
| **Pre-built (recommended)** | `docker compose pull && docker compose up -d` | Using GHCR image |
| **Build from source** | `git pull && docker compose up -d --build` | Uncommented `build: .` in docker-compose.yaml |

### Version-Specific Steps

#### v1.3.0 → v1.4.0

This update adds the **Metrics Dashboard** (:8383) with Prometheus charts. It's optional — your existing setup continues to work without any config changes.

**New features:**
- Metrics Dashboard with 27 Prometheus charts
- Prometheus container (auto-starts with `docker compose up`)
- Time range selector per chart section (1m, 5m, 30m, 1h, All)
- Per-method RPC latency charts
- NVMe temperature and wear level monitoring
- Purple dark theme across both dashboards

**Steps:**

**On your validator server:**

```bash
# 1. Update the TrieDB collector script (adds NVMe SMART metrics)
curl -o ~/monad-monitoring/scripts/triedb-collector.sh \
  https://raw.githubusercontent.com/MictoNode/micto-monad-monitor/main/scripts/triedb-collector.sh
chmod +x ~/monad-monitoring/scripts/triedb-collector.sh

# 2. Recreate node-exporter with --network=host (required for correct interface names)
docker stop node-exporter && docker rm node-exporter
docker run -d \
  --name node-exporter \
  --restart unless-stopped \
  --network=host \
  -v /proc:/host/proc:ro \
  -v /sys:/host/sys:ro \
  -v /:/rootfs:ro \
  -v /var/lib/node_exporter/textfile_collector:/textfile_collector \
  prom/node-exporter:latest \
  --path.procfs=/host/proc \
  --path.sysfs=/host/sys \
  --path.rootfs=/rootfs \
  --web.listen-address=:9100 \
  --collector.textfile.directory=/textfile_collector
```

> **Note:** To use a different port, change `--web.listen-address=:9100` to your desired port (e.g. `:9200`).
> Don't forget to also update `node_exporter_port` in `validators.yaml` to match.

**On your monitor server:**

```bash
# 3. Pull updated config files (required — adds Prometheus service + config)
cd ~/micto-monad-monitor    # your monitor directory
git pull

# 4. Pull latest image
docker compose pull

# 5. (Optional) Enable Metrics Dashboard — add to your .env:
#    DASHBOARD_PASSWORD=your_secure_password
#    DASHBOARD_JWT_SECRET=<generate with: openssl rand -hex 32>
nano .env

# 6. Start all services (monitor + Prometheus)
docker compose up -d

# 7. Verify — check that both containers are running:
docker compose ps
```

**What changes automatically:**
- Prometheus container starts and scrapes `:8889` and `:9100`
- Prometheus data stored in Docker volume (`prometheus-data`), 30-day retention
- Monitor Dashboard (:8282) and Health Server (:8181) unchanged

**No action needed if you don't want the Metrics Dashboard** — it stays disabled when `DASHBOARD_PASSWORD` is empty.

---

## Support

- **Issues:** [GitHub Issues](https://github.com/MictoNode/micto-monad-monitor/issues)

---

*Made by [MictoNode](https://mictonode.com) - Sleep better knowing your validators are watched.*
