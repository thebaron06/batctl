#!/bin/bash
set -e

CONFIG="${BATCTL_CONFIG:-/config/batctl.conf}"

if [ $# -eq 0 ]; then
    # Default: start the cron scheduler.
    if [ ! -f "$CONFIG" ]; then
        echo "ERROR: config not found at $CONFIG" >&2
        echo "Mount your config with: -v /path/to/batctl.conf:$CONFIG:ro" >&2
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
