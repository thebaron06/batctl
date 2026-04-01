#!/bin/env bash

set -euo pipefail

HOST="fronius-wr.home"
CONFIG="charge_rules.conf"

SCRIPT_DIR="$(dirname "$0")"

cd "$SCRIPT_DIR"        # go to script-path
# optional: source venv (not strictly required if you call the venv python directly)
# . ./venv/bin/activate

export PYTHONUNBUFFERED=1
${SCRIPT_DIR}/.venv/bin/python3 batctl.py --host "${HOST}" --config "${CONFIG}" 2>&1 | /usr/bin/logger -t batctl -p user.info
