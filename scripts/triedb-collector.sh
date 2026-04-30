#!/bin/bash
#
# TrieDB (MonadDB) + NVMe SMART Metrics Collector for Node Exporter Textfile Collector
# This script collects Monad TrieDB disk usage and NVMe SMART metrics and exports them
# in Prometheus format for node-exporter to scrape.
#
# Requirements:
#   - monad-mpt binary in PATH or /usr/local/bin/
#   - nvme-cli installed (nvme smart-log command)
#   - node-exporter with textfile collector enabled
#   - Write access to /var/lib/node_exporter/textfile_collector/
#
# Installation:
#   1. Copy this script to /home/monad/monad-monitoring/scripts/
#   2. Make executable: chmod +x triedb-collector.sh
#   3. Add to crontab: * * * * * /home/monad/monad-monitoring/scripts/triedb-collector.sh >> /var/log/triedb-collector.log 2>&1
#

set -euo pipefail

# Configuration
TARGET_DRIVE="${TARGET_DRIVE:-triedb}"
MONAD_MPT="${MONAD_MPT:-/usr/local/bin/monad-mpt}"
OUTPUT_DIR="${OUTPUT_DIR:-/var/lib/node_exporter/textfile_collector}"
OUTPUT_FILE="${OUTPUT_DIR}/monad_triedb.prom"
METRIC_PREFIX="monad_triedb"

# Check if monad-mpt exists
if [[ ! -x "$MONAD_MPT" ]]; then
    echo "ERROR: monad-mpt not found at $MONAD_MPT" >&2
    exit 1
fi

# Check if output directory exists
if [[ ! -d "$OUTPUT_DIR" ]]; then
    echo "ERROR: Output directory $OUTPUT_DIR does not exist" >&2
    echo "Create it with: sudo mkdir -p $OUTPUT_DIR && sudo chown \$USER:\$USER $OUTPUT_DIR" >&2
    exit 1
fi

# Check if we can write to the directory
if [[ ! -w "$OUTPUT_DIR" ]]; then
    echo "ERROR: Cannot write to $OUTPUT_DIR" >&2
    echo "Fix with: sudo chown \$USER:\$USER $OUTPUT_DIR" >&2
    exit 1
fi

# Function to convert size string to bytes
convert_to_bytes() {
    local size_str="$1"
    local size_value
    local size_unit

    # Extract number and unit
    size_value=$(echo "$size_str" | grep -oE '[0-9.]+')
    size_unit=$(echo "$size_str" | grep -oE '[A-Za-z]+')

    case "${size_unit,,}" in
        tb|t)
            echo "$size_value * 1024 * 1024 * 1024 * 1024" | bc | awk '{printf "%.0f", $0}'
            ;;
        gb|g)
            echo "$size_value * 1024 * 1024 * 1024" | bc | awk '{printf "%.0f", $0}'
            ;;
        mb|m)
            echo "$size_value * 1024 * 1024" | bc | awk '{printf "%.0f", $0}'
            ;;
        kb|k)
            echo "$size_value * 1024" | bc | awk '{printf "%.0f", $0}'
            ;;
        b)
            echo "$size_value" | awk '{printf "%.0f", $0}'
            ;;
        *)
            echo "0"
            ;;
    esac
}

# Run monad-mpt and capture output
MPT_OUTPUT=$("$MONAD_MPT" --storage "/dev/$TARGET_DRIVE" 2>&1) || {
    echo "ERROR: Failed to run monad-mpt: $MPT_OUTPUT" >&2
    exit 1
}

# Parse the output
# Expected format:
#         Capacity           Used      %  Path
#          3.49 Tb       24.30 Gb  0.68%  "/dev/nvme1n1p1"

# Extract capacity (first value in the row after header)
CAPACITY_STR=$(echo "$MPT_OUTPUT" | grep -A1 'Capacity' | tail -1 | awk '{print $1 " " $2}' | tr -d '\r')
USED_STR=$(echo "$MPT_OUTPUT" | grep -A1 'Capacity' | tail -1 | awk '{print $3 " " $4}' | tr -d '\r')
USED_PERCENT=$(echo "$MPT_OUTPUT" | grep -A1 'Capacity' | tail -1 | awk '{print $5}' | tr -d '%' | tr -d '\r')

# Convert to bytes
CAPACITY_BYTES=$(convert_to_bytes "$CAPACITY_STR")
USED_BYTES=$(convert_to_bytes "$USED_STR")

# Calculate available bytes
if [[ "$CAPACITY_BYTES" -gt 0 && "$USED_BYTES" -gt 0 ]]; then
    AVAIL_BYTES=$((CAPACITY_BYTES - USED_BYTES))
else
    AVAIL_BYTES=0
fi

# Parse additional MPT database info if available
# Looking for lines like:
#   Fast: 94 chunks with capacity 23.50 Gb used 23.40 Gb
#   Slow: 3 chunks with capacity 768.00 Mb used 658.72 Mb
#   Free: 14207 chunks with capacity 3.47 Tb used 0.00 bytes

FAST_CHUNKS=0
FAST_CAPACITY=0
FAST_USED=0
SLOW_CHUNKS=0
SLOW_CAPACITY=0
SLOW_USED=0
FREE_CHUNKS=0
FREE_CAPACITY=0

# Parse Fast chunks
if FAST_LINE=$(echo "$MPT_OUTPUT" | grep 'Fast:'); then
    FAST_CHUNKS=$(echo "$FAST_LINE" | grep -oE '[0-9]+ chunks' | grep -oE '[0-9]+' | head -1)
    FAST_CAPACITY=$(convert_to_bytes "$(echo "$FAST_LINE" | grep -oE 'capacity [0-9.]+ [A-Za-z]+' | awk '{print $2 " " $3}')")
    FAST_USED=$(convert_to_bytes "$(echo "$FAST_LINE" | grep -oE 'used [0-9.]+ [A-Za-z]+' | awk '{print $2 " " $3}')")
fi

# Parse Slow chunks
if SLOW_LINE=$(echo "$MPT_OUTPUT" | grep 'Slow:'); then
    SLOW_CHUNKS=$(echo "$SLOW_LINE" | grep -oE '[0-9]+ chunks' | grep -oE '[0-9]+' | head -1)
    SLOW_CAPACITY=$(convert_to_bytes "$(echo "$SLOW_LINE" | grep -oE 'capacity [0-9.]+ [A-Za-z]+' | awk '{print $2 " " $3}')")
    SLOW_USED=$(convert_to_bytes "$(echo "$SLOW_LINE" | grep -oE 'used [0-9.]+ [A-Za-z]+' | awk '{print $2 " " $3}')")
fi

# Parse Free chunks
if FREE_LINE=$(echo "$MPT_OUTPUT" | grep 'Free:'); then
    FREE_CHUNKS=$(echo "$FREE_LINE" | grep -oE '[0-9]+ chunks' | grep -oE '[0-9]+' | head -1)
    FREE_CAPACITY=$(convert_to_bytes "$(echo "$FREE_LINE" | grep -oE 'capacity [0-9.]+ [A-Za-z]+' | awk '{print $2 " " $3}')")
fi

# Parse history retention info
HISTORY_COUNT=0
HISTORY_EARLIEST=0
HISTORY_LATEST=0
HISTORY_MAX=0

if HISTORY_LINE=$(echo "$MPT_OUTPUT" | grep 'history'); then
    HISTORY_COUNT=$(echo "$HISTORY_LINE" | grep -oE '[0-9]+ history' | grep -oE '[0-9]+' | head -1 || echo "0")
    HISTORY_MAX=$(echo "$MPT_OUTPUT" | grep 'retain no more than' | grep -oE '[0-9]+' | tail -1 || echo "0")
fi

# Get current timestamp
TIMESTAMP=$(date +%s%3N)

# Write metrics to temp file first (atomic write)
TEMP_FILE=$(mktemp "${OUTPUT_DIR}/.monad_triedb.prom.XXXXXX")

{
    # Total capacity
    printf "# HELP %s_capacity_bytes Total capacity of TrieDB storage\n" "$METRIC_PREFIX"
    printf "# TYPE %s_capacity_bytes gauge\n" "$METRIC_PREFIX"
    printf "%s_capacity_bytes{drive=\"%s\"} %s\n" "$METRIC_PREFIX" "$TARGET_DRIVE" "$CAPACITY_BYTES"

    # Used capacity
    printf "# HELP %s_used_bytes Used capacity of TrieDB storage\n" "$METRIC_PREFIX"
    printf "# TYPE %s_used_bytes gauge\n" "$METRIC_PREFIX"
    printf "%s_used_bytes{drive=\"%s\"} %s\n" "$METRIC_PREFIX" "$TARGET_DRIVE" "$USED_BYTES"

    # Available capacity
    printf "# HELP %s_avail_bytes Available capacity of TrieDB storage\n" "$METRIC_PREFIX"
    printf "# TYPE %s_avail_bytes gauge\n" "$METRIC_PREFIX"
    printf "%s_avail_bytes{drive=\"%s\"} %s\n" "$METRIC_PREFIX" "$TARGET_DRIVE" "$AVAIL_BYTES"

    # Usage percentage
    printf "# HELP %s_used_percent Percentage of TrieDB storage used\n" "$METRIC_PREFIX"
    printf "# TYPE %s_used_percent gauge\n" "$METRIC_PREFIX"
    printf "%s_used_percent{drive=\"%s\"} %s\n" "$METRIC_PREFIX" "$TARGET_DRIVE" "$USED_PERCENT"

    # Fast chunks metrics
    if [[ "$FAST_CHUNKS" -gt 0 ]]; then
        printf "# HELP %s_fast_chunks Number of fast chunks in TrieDB\n" "$METRIC_PREFIX"
        printf "# TYPE %s_fast_chunks gauge\n" "$METRIC_PREFIX"
        printf "%s_fast_chunks %s\n" "$METRIC_PREFIX" "$FAST_CHUNKS"

        printf "# HELP %s_fast_capacity_bytes Capacity of fast chunks in bytes\n" "$METRIC_PREFIX"
        printf "# TYPE %s_fast_capacity_bytes gauge\n" "$METRIC_PREFIX"
        printf "%s_fast_capacity_bytes %s\n" "$METRIC_PREFIX" "$FAST_CAPACITY"

        printf "# HELP %s_fast_used_bytes Used bytes in fast chunks\n" "$METRIC_PREFIX"
        printf "# TYPE %s_fast_used_bytes gauge\n" "$METRIC_PREFIX"
        printf "%s_fast_used_bytes %s\n" "$METRIC_PREFIX" "$FAST_USED"
    fi

    # Slow chunks metrics
    if [[ "$SLOW_CHUNKS" -gt 0 ]]; then
        printf "# HELP %s_slow_chunks Number of slow chunks in TrieDB\n" "$METRIC_PREFIX"
        printf "# TYPE %s_slow_chunks gauge\n" "$METRIC_PREFIX"
        printf "%s_slow_chunks %s\n" "$METRIC_PREFIX" "$SLOW_CHUNKS"

        printf "# HELP %s_slow_capacity_bytes Capacity of slow chunks in bytes\n" "$METRIC_PREFIX"
        printf "# TYPE %s_slow_capacity_bytes gauge\n" "$METRIC_PREFIX"
        printf "%s_slow_capacity_bytes %s\n" "$METRIC_PREFIX" "$SLOW_CAPACITY"

        printf "# HELP %s_slow_used_bytes Used bytes in slow chunks\n" "$METRIC_PREFIX"
        printf "# TYPE %s_slow_used_bytes gauge\n" "$METRIC_PREFIX"
        printf "%s_slow_used_bytes %s\n" "$METRIC_PREFIX" "$SLOW_USED"
    fi

    # Free chunks metrics
    if [[ "$FREE_CHUNKS" -gt 0 ]]; then
        printf "# HELP %s_free_chunks Number of free chunks in TrieDB\n" "$METRIC_PREFIX"
        printf "# TYPE %s_free_chunks gauge\n" "$METRIC_PREFIX"
        printf "%s_free_chunks %s\n" "$METRIC_PREFIX" "$FREE_CHUNKS"

        printf "# HELP %s_free_capacity_bytes Capacity of free chunks in bytes\n" "$METRIC_PREFIX"
        printf "# TYPE %s_free_capacity_bytes gauge\n" "$METRIC_PREFIX"
        printf "%s_free_capacity_bytes %s\n" "$METRIC_PREFIX" "$FREE_CAPACITY"
    fi

    # History retention metrics
    if [[ "$HISTORY_COUNT" -gt 0 ]]; then
        printf "# HELP %s_history_count Number of block history retained\n" "$METRIC_PREFIX"
        printf "# TYPE %s_history_count gauge\n" "$METRIC_PREFIX"
        printf "%s_history_count %s\n" "$METRIC_PREFIX" "$HISTORY_COUNT"

        if [[ "$HISTORY_MAX" -gt 0 ]]; then
            printf "# HELP %s_history_max Maximum blocks to retain\n" "$METRIC_PREFIX"
            printf "# TYPE %s_history_max gauge\n" "$METRIC_PREFIX"
            printf "%s_history_max %s\n" "$METRIC_PREFIX" "$HISTORY_MAX"
        fi
    fi

    # ── NVMe SMART metrics (wear level + temperature) ──
    # Discover NVMe namespace devices dynamically, skip partitions
    # Works with any naming: nvme0n1, nvme1n1, nvme-eui.xxxxx, etc.
    mapfile -t NVME_DEVS < <(lsblk -d -n -o NAME -I 259 2>/dev/null | grep -vE 'p[0-9]+$')

    NVME_WEAR_LINES=()
    NVME_TEMP_LINES=()
    for dev_name in "${NVME_DEVS[@]}"; do
        [[ -n "$dev_name" ]] || continue
        dev="/dev/$dev_name"
        [[ -b "$dev" ]] || continue
        smart_output=$(nvme smart-log "$dev" 2>/dev/null) || continue

        # percentage_used (wear level) — 0% = brand new, 100% = end of life
        pct_used=$(echo "$smart_output" | grep 'percentage_used' | grep -oE '[0-9]+' | head -1)
        if [[ -n "$pct_used" ]]; then
            pct_ratio=$(awk "BEGIN {printf \"%.4f\", $pct_used / 100}")
            NVME_WEAR_LINES+=("nvme_percentage_used_ratio{device=\"$dev_name\"} $pct_ratio")
        fi

        # Temperature from SMART log (format: "temperature : 27 °C (300 K)")
        temp=$(echo "$smart_output" | grep '^temperature' | grep -oE '[0-9]+' | head -1)
        if [[ -n "$temp" ]]; then
            NVME_TEMP_LINES+=("nvme_temperature_celsius{device=\"$dev_name\"} $temp")
        fi
    done

    # Print wear level metrics (grouped)
    if [[ ${#NVME_WEAR_LINES[@]} -gt 0 ]]; then
        printf "# HELP nvme_percentage_used_ratio NVMe SSD wear level (0=brand new, 1=end of life)\n"
        printf "# TYPE nvme_percentage_used_ratio gauge\n"
        for line in "${NVME_WEAR_LINES[@]}"; do
            printf "%s\n" "$line"
        done
    fi

    # Print temperature metrics (grouped)
    if [[ ${#NVME_TEMP_LINES[@]} -gt 0 ]]; then
        printf "# HELP nvme_temperature_celsius NVMe SSD temperature from SMART log\n"
        printf "# TYPE nvme_temperature_celsius gauge\n"
        for line in "${NVME_TEMP_LINES[@]}"; do
            printf "%s\n" "$line"
        done
    fi

} > "$TEMP_FILE"

# Atomic move to final location
mv "$TEMP_FILE" "$OUTPUT_FILE"

# Fix permissions so node-exporter (running as nobody/65534) can read the file
chmod 644 "$OUTPUT_FILE"

echo "$(date '+%Y-%m-%d %H:%M:%S') - TrieDB metrics collected: ${USED_PERCENT}% used (${USED_BYTES}/${CAPACITY_BYTES} bytes) | NVMe SMART collected"
