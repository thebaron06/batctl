#!/bin/bash
set -e

CONFIG="${BATCTL_CONFIG:-/config/batctl.conf}"

if [ $# -eq 0 ]; then
    # Default: start the cron scheduler.
    # Accept if the default config exists OR if a schedule file exists
    # (schedule-only setups may use different config filenames).
    if [ ! -f "$CONFIG" ] && [ ! -f "/config/.schedule.json" ]; then
        echo "ERROR: no config found at $CONFIG and no .schedule.json in /config" >&2
        echo "Either mount a batctl.conf or create a schedule via the web UI." >&2
        exit 1
    fi
    echo "batctl: starting scheduler (every 5 minutes)"
    exec cron -f
else
    # Pass-through: run a single batctl subcommand and exit.
    # Examples:
    #   docker run --rm batctl run --config /config/batctl.conf --dry-run
    #   docker run --rm batctl detect --config /config/batctl.conf
    exec /app/.venv/bin/python /app/batctl.py "$@"
fi
