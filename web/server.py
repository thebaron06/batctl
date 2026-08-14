#!/usr/bin/env python3
"""Flask web server for the batctl dashboard and configuration UI."""

import json
import os
import re
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, render_template, abort

app = Flask(__name__, template_folder="templates")

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/var/lib/batctl/state.json"))
WEATHER_CACHE = Path(os.environ.get("WEATHER_CACHE", "/var/lib/batctl/weather_cache.json"))
EXAMPLE_FILE = Path("/app/batctl.conf.example")
SCHEDULE_FILE = CONFIG_DIR / ".schedule.json"

FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.conf$")


def _valid_conf_name(name):
    return bool(FILENAME_RE.match(name)) and "/" not in name and ".." not in name


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _active_entry(entries, month):
    for entry in entries:
        if month in entry.get("months", []):
            return entry
    return None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    now = datetime.now()
    state = _read_json(STATE_FILE)
    weather = _read_json(WEATHER_CACHE)
    schedule_data = _read_json(SCHEDULE_FILE)
    entries = schedule_data.get("entries", []) if schedule_data else []
    active = _active_entry(entries, now.month)
    return jsonify({
        "current_time": now.isoformat(),
        "current_month": now.month,
        "state": state,
        "weather": weather,
        "schedule": schedule_data,
        "active_entry": active,
    })


@app.route("/api/configs")
def api_configs():
    files = []
    for p in sorted(CONFIG_DIR.glob("*.conf")):
        stat = p.stat()
        files.append({
            "name": p.name,
            "size": stat.st_size,
            "modified_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return jsonify(files)


@app.route("/api/configs/<filename>", methods=["GET", "PUT", "DELETE"])
def api_config_file(filename):
    if not _valid_conf_name(filename):
        abort(400, description="Invalid filename")
    path = CONFIG_DIR / filename

    if request.method == "GET":
        if not path.exists():
            abort(404)
        return jsonify({"name": filename, "content": path.read_text()})

    if request.method == "PUT":
        body = request.get_json(force=True, silent=True) or {}
        content = body.get("content", "")
        path.write_text(content)
        return jsonify({"ok": True})

    if request.method == "DELETE":
        if not path.exists():
            abort(404)
        path.unlink()
        return jsonify({"ok": True})


@app.route("/api/example")
def api_example():
    if not EXAMPLE_FILE.exists():
        return jsonify({"content": ""})
    return jsonify({"content": EXAMPLE_FILE.read_text()})


@app.route("/api/schedule", methods=["GET", "PUT"])
def api_schedule():
    if request.method == "GET":
        data = _read_json(SCHEDULE_FILE)
        if data is None:
            data = {"entries": []}
        return jsonify(data)

    body = request.get_json(force=True, silent=True) or {}
    SCHEDULE_FILE.write_text(json.dumps(body, indent=2))
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
