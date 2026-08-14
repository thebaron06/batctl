#!/usr/bin/env python3
"""
Month-based cron dispatcher for batctl.
Reads /config/.schedule.json and dispatches to the correct batctl command.
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

BATCTL_PATH = Path(__file__).parent / "batctl.py"
SCHEDULE_PATH = Path("/config/.schedule.json")
# Prefer the persistent data volume so the flag survives container restarts.
# Fall back to /tmp if the volume is not mounted (e.g. during local testing).
FLAG_DIR = Path("/var/lib/batctl") if Path("/var/lib/batctl").is_dir() else Path("/tmp")


def run_batctl(*args):
    os.execv(sys.executable, [sys.executable, str(BATCTL_PATH)] + list(args))


def main():
    now = datetime.now()
    current_month = now.month

    if not SCHEDULE_PATH.exists():
        log.info("No schedule file found, running default batctl run")
        run_batctl("run", "--config", "/config/batctl.conf")
        return

    try:
        data = json.loads(SCHEDULE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to read schedule: %s", exc)
        sys.exit(1)

    entries = data.get("entries", [])
    matched = None
    for entry in entries:
        if current_month in entry.get("months", []):
            matched = entry
            break

    if matched is None:
        log.info("No schedule entry for month %d, doing nothing", current_month)
        sys.exit(0)

    config = matched.get("config", "batctl.conf")
    mode = matched.get("mode", "run")
    config_path = f"/config/{config}"

    if mode == "run":
        log.info("Month %d: run mode, config=%s", current_month, config)
        run_batctl("run", "--config", config_path)

    elif mode == "restore_once":
        flag_name = now.strftime("batctl_restore_done_%Y-%m")
        flag_path = FLAG_DIR / flag_name
        if flag_path.exists():
            log.info("Month %d: restore_once already done (%s)", current_month, flag_name)
            sys.exit(0)
        log.info("Month %d: restore_once, touching %s, config=%s", current_month, flag_path, config)
        flag_path.touch()
        run_batctl("run", "--restore", "--config", config_path)

    else:
        log.error("Unknown mode %r in schedule entry", mode)
        sys.exit(1)


if __name__ == "__main__":
    main()
