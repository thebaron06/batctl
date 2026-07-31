#!/usr/bin/env python3
"""
batctl — Battery charge/discharge control for Fronius Gen24 via SunSpec Modbus TCP.

Reads state of charge from a SunSpec hybrid inverter/storage device and controls
the battery charge and discharge rate according to configurable thresholds,
time-of-day phases, and optional solar production forecasts.

SunSpec models used for control:
  1   – Common (identification logging)
  124 – Basic Storage Controls (StorCtl_Mod, InWRte, OutWRte, ChaState, WChaMax)

SunSpec models used for detection only (detect subcommand):
  120 – Nameplate (WHRtg → battery energy capacity)

Subcommands:
  run      — control charge/discharge; called periodically by cron/systemd (default)
  detect   — query the device once and write static info (capacity, identity) into
             the config file; re-run whenever hardware changes
  forecast — fetch tomorrow's solar production forecast and write a local cache file;
             run automatically by 'run' when the cache is missing or stale; can also
             be scheduled independently (e.g. a few times a day via cron)

Modbus register notes (model 124, Int&SF storage ROW):
  StorCtl_Mod  bit 0 = charge limit active, bit 1 = discharge limit active
  InWRte       max charge rate  as % of WChaMax (with InOutWRte_SF scale factor)
  OutWRte      max discharge rate as % of WChaMax (with InOutWRte_SF scale factor)
  Valid InWRte/OutWRte range: -100.00 % .. +100.00 %

  NEVER set StorCtl_Mod = 3 (both bits) with both InWRte and OutWRte negative;
  that triggers Modbus exception 3 (ILLEGAL DATA VALUE).  This code only ever
  sets StorCtl_Mod to 0, 1 (charge limit), or 2 (discharge limit).
"""

import argparse
import configparser
import datetime
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from astral import LocationInfo
from astral.sun import sun
from zoneinfo import ZoneInfo

import sunspec2.modbus.client as sunspec_client

# ---------------------------------------------------------------------------
# Logging — timestamp always included (cron-friendly)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config defaults and dataclass
# ---------------------------------------------------------------------------

_CONFIG_DEFAULTS: dict[str, dict[str, str]] = {
    "connection": {
        "host": "",
        "port": "502",
        "slave_id": "1",
    },
    "features": {
        "charge_limit": "false",
        "end_of_day_full_charge": "false",
        "night_grid_export": "false",
        "weather_aware": "false",
    },
    "charging": {
        "upper_soc_limit": "80",
        "min_charge_rate_w": "300",
    },
    # [charge_taper] has no fixed defaults; user provides threshold = scale entries
    "end_of_day": {
        "lead_time_minutes": "120",
    },
    "grid_export": {
        "reserve_soc": "12",
        "min_discharge_rate_w": "300",
    },
    "battery": {
        "capacity_kwh": "0",
        "wchamax_w": "0",
    },
    "location": {
        "latitude": "",
        "longitude": "",
        "timezone": "",
    },
    "weather": {
        "pv_peak_kwp": "0",
        "performance_ratio": "0.75",
        "skip_export_below_kwh": "10",
        "refresh_hours": "6",
        "cache_path": "/tmp/batctl_weather_cache.json",
    },
    "detected": {
        "manufacturer": "",
        "model": "",
        "serial": "",
        "firmware": "",
    },
}


@dataclass
class Config:
    """Typed representation of the INI configuration."""
    # [connection]
    host: str = ""
    port: int = 502
    slave_id: int = 1
    # [features]
    feat_charge_limit: bool = False
    feat_end_of_day_full_charge: bool = False
    feat_night_grid_export: bool = False
    feat_weather_aware: bool = False
    # [charging]
    upper_soc_limit: float = 80.0
    min_charge_rate_w: float = 300.0
    # [charge_taper]: sorted descending by threshold
    charge_taper: list = field(default_factory=list)
    # [end_of_day]
    lead_time_minutes: int = 120
    # [grid_export]
    reserve_soc: float = 12.0
    min_discharge_rate_w: float = 300.0
    # [battery]
    capacity_kwh: float = 0.0
    wchamax_w: float = 0.0
    # [location]
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: str = ""
    # [weather]
    pv_peak_kwp: float = 0.0
    performance_ratio: float = 0.75
    skip_export_below_kwh: float = 10.0
    refresh_hours: float = 6.0
    cache_path: str = "/tmp/batctl_weather_cache.json"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _make_parser() -> configparser.ConfigParser:
    """Return a ConfigParser pre-seeded with application defaults."""
    cp = configparser.ConfigParser(inline_comment_prefixes=('#', ';'))
    for section, kvs in _CONFIG_DEFAULTS.items():
        cp.add_section(section)
        for k, v in kvs.items():
            cp.set(section, k, v)
    cp.add_section("charge_taper")
    return cp


def load_config(path: str, overrides: Optional[dict] = None) -> Config:
    """Load the INI config file and apply optional CLI overrides.

    overrides: mapping of 'section.key' → string value, derived from CLI args
    whose dest was set to 'section.key'.  Values of None are ignored.
    """
    cp = _make_parser()
    cp.read(path)  # silently ignored if file is absent

    for dotkey, val in (overrides or {}).items():
        if val is None:
            continue
        section, key = dotkey.split(".", 1)
        if not cp.has_section(section):
            cp.add_section(section)
        cp.set(section, key, str(val))

    taper: list[tuple[float, float]] = []
    for k, v in cp.items("charge_taper"):
        try:
            threshold, scale = float(k), float(v)
        except ValueError:
            log.warning("Skipping invalid charge_taper entry: %s = %s", k, v)
            continue
        if not (0.0 <= threshold <= 100.0 and 0.0 <= scale <= 1.0):
            log.warning("Skipping out-of-range charge_taper entry: %s = %s", k, v)
            continue
        taper.append((threshold, scale))
    taper.sort(key=lambda t: t[0], reverse=True)

    lat_s = cp.get("location", "latitude")
    lon_s = cp.get("location", "longitude")

    return Config(
        host=cp.get("connection", "host"),
        port=cp.getint("connection", "port"),
        slave_id=cp.getint("connection", "slave_id"),
        feat_charge_limit=cp.getboolean("features", "charge_limit"),
        feat_end_of_day_full_charge=cp.getboolean("features", "end_of_day_full_charge"),
        feat_night_grid_export=cp.getboolean("features", "night_grid_export"),
        feat_weather_aware=cp.getboolean("features", "weather_aware"),
        upper_soc_limit=cp.getfloat("charging", "upper_soc_limit"),
        min_charge_rate_w=cp.getfloat("charging", "min_charge_rate_w"),
        charge_taper=taper,
        lead_time_minutes=cp.getint("end_of_day", "lead_time_minutes"),
        reserve_soc=cp.getfloat("grid_export", "reserve_soc"),
        min_discharge_rate_w=cp.getfloat("grid_export", "min_discharge_rate_w"),
        capacity_kwh=cp.getfloat("battery", "capacity_kwh"),
        wchamax_w=cp.getfloat("battery", "wchamax_w"),
        latitude=float(lat_s) if lat_s else None,
        longitude=float(lon_s) if lon_s else None,
        timezone=cp.get("location", "timezone"),
        pv_peak_kwp=cp.getfloat("weather", "pv_peak_kwp"),
        performance_ratio=cp.getfloat("weather", "performance_ratio"),
        skip_export_below_kwh=cp.getfloat("weather", "skip_export_below_kwh"),
        refresh_hours=cp.getfloat("weather", "refresh_hours"),
        cache_path=cp.get("weather", "cache_path"),
    )


def update_config_file(path: str, updates: dict[str, dict[str, str]]) -> None:
    """Update specific section keys in an INI file, preserving all other content.

    Existing keys are updated in-place (comments preserved).  Keys absent from
    the file are appended.  New sections are added at the end.

    updates = {'section_name': {'key': 'value', ...}, ...}
    """
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    pending: dict[str, dict[str, str]] = {s: dict(kv) for s, kv in updates.items()}
    result: list[str] = []
    current_section: Optional[str] = None

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("[") and "]" in stripped:
            # Flush leftover keys for the section we're leaving
            if current_section and current_section in pending and pending[current_section]:
                for k, v in pending.pop(current_section).items():
                    result.append(f"{k} = {v}\n")
            current_section = stripped[1 : stripped.index("]")]
            result.append(line)
            continue

        if (
            current_section
            and current_section in pending
            and "=" in stripped
            and not stripped.startswith(("#", ";"))
        ):
            key = stripped.split("=", 1)[0].strip()
            if key in pending[current_section]:
                result.append(f"{key} = {pending[current_section].pop(key)}\n")
                continue

        result.append(line)

    # Flush pending keys for the last section in the file
    if current_section and current_section in pending and pending[current_section]:
        for k, v in pending.pop(current_section).items():
            result.append(f"{k} = {v}\n")

    # Append sections that didn't exist in the file
    for section, kvs in pending.items():
        if kvs:
            result.append(f"\n[{section}]\n")
            for k, v in kvs.items():
                result.append(f"{k} = {v}\n")

    with open(path, "w") as f:
        f.writelines(result)


# ---------------------------------------------------------------------------
# Feature compatibility validation
# ---------------------------------------------------------------------------

def validate_features(cfg: Config) -> None:
    """Raise ValueError with a clear message when feature flags are misconfigured.

    Called early in 'run' so misconfigurations fail fast with actionable messages.
    """
    has_location = (
        cfg.latitude is not None
        and cfg.longitude is not None
        and cfg.timezone
    )

    if cfg.feat_end_of_day_full_charge:
        if not cfg.feat_charge_limit:
            raise ValueError(
                "[features] end_of_day_full_charge = true requires charge_limit = true"
            )
        if not has_location:
            raise ValueError(
                "[features] end_of_day_full_charge requires [location] "
                "latitude, longitude, and timezone to be set"
            )

    if cfg.feat_night_grid_export:
        if not has_location:
            raise ValueError(
                "[features] night_grid_export requires [location] "
                "latitude, longitude, and timezone to be set"
            )
        if cfg.capacity_kwh <= 0:
            raise ValueError(
                "[features] night_grid_export requires [battery] capacity_kwh > 0 "
                "(run: batctl detect --config <file>)"
            )

    if cfg.feat_weather_aware:
        if not cfg.feat_night_grid_export:
            raise ValueError(
                "[features] weather_aware = true requires night_grid_export = true"
            )
        if cfg.pv_peak_kwp <= 0:
            raise ValueError(
                "[features] weather_aware requires [weather] pv_peak_kwp > 0"
            )
        if not (0.0 < cfg.performance_ratio <= 1.0):
            raise ValueError(
                "[features] weather_aware requires [weather] performance_ratio in (0, 1]"
            )


# ---------------------------------------------------------------------------
# Sun times
# ---------------------------------------------------------------------------

def get_sun_times(lat: float, lon: float, tz_name: str, for_date: datetime.date) -> dict:
    """Return astral sun dict for the given location and date.

    Keys include 'sunrise' and 'sunset' as tz-aware datetime objects.
    """
    loc = LocationInfo(latitude=lat, longitude=lon)
    return sun(loc.observer, date=for_date, tzinfo=ZoneInfo(tz_name))


# ---------------------------------------------------------------------------
# Phase determination
# ---------------------------------------------------------------------------

PHASE_DAY = "day"
PHASE_END_OF_DAY = "end_of_day"
PHASE_NIGHT = "night"


def determine_phase(
    now: datetime.datetime,
    sunrise: datetime.datetime,
    sunset: datetime.datetime,
    lead_minutes: int,
) -> str:
    """Determine the control phase from the current time.

    All arguments must be tz-aware datetimes for the same solar day.
    'sunrise'/'sunset' are today's values; before-sunrise is treated as
    still being in last night's NIGHT phase.

    Returns one of PHASE_DAY, PHASE_END_OF_DAY, PHASE_NIGHT.
    """
    eod_start = sunset - datetime.timedelta(minutes=lead_minutes)
    if now < sunrise:
        return PHASE_NIGHT      # still last night's cycle
    if now < eod_start:
        return PHASE_DAY
    if now < sunset:
        return PHASE_END_OF_DAY
    return PHASE_NIGHT


# ---------------------------------------------------------------------------
# Charge-rate decision (pure logic, testable without I/O)
# ---------------------------------------------------------------------------

def compute_charge_setpoint(
    soc: float,
    taper: list[tuple[float, float]],
    upper_soc_limit: float,
    min_charge_rate_w: float,
    wchamax_w: float,
    sf: int,
    feat_charge_limit: bool,
) -> tuple[int, int]:
    """Compute (InWRte_raw, StorCtl_Mod) for a charging phase.

    sf is the value read from InOutWRte_SF (typically -2, meaning raw = pct × 100).
    Returns raw register values ready to write to the device.
    """
    sf_abs = abs(sf)
    max_raw = int(100 * (10 ** sf_abs))

    if feat_charge_limit and soc >= upper_soc_limit:
        # Upper SOC limit reached: stop charging
        return 0, 1

    # Apply taper rules (descending threshold order, first match wins)
    matched_scale: Optional[float] = None
    for threshold, scale in taper:
        if soc >= threshold:
            matched_scale = scale
            break

    if matched_scale is None:
        # SOC below all taper thresholds: full charge rate, no limit active
        return max_raw, 0

    min_rate_pct = (min_charge_rate_w / wchamax_w * 100.0) if wchamax_w > 0 else 0.0
    rate_pct = max(min_rate_pct, matched_scale * 100.0)
    return int(rate_pct * (10 ** sf_abs)), 1


# ---------------------------------------------------------------------------
# Discharge-rate calculation (pure logic, testable without I/O)
# ---------------------------------------------------------------------------

def compute_discharge_setpoint(
    soc: float,
    reserve_soc: float,
    capacity_kwh: float,
    hours_left: float,
    wchamax_w: float,
    min_discharge_w: float,
    sf: int,
) -> Optional[int]:
    """Compute OutWRte raw register value for the night export phase.

    Returns None when discharge should stop (SOC at/below reserve or no time left).
    Otherwise returns the raw register value to write to OutWRte.
    """
    if soc <= reserve_soc or hours_left <= 0.0 or wchamax_w <= 0.0:
        return None

    energy_kwh = (soc - reserve_soc) / 100.0 * capacity_kwh
    required_w = energy_kwh * 1000.0 / hours_left
    min_pct = min_discharge_w / wchamax_w * 100.0
    rate_pct = min(max(required_w / wchamax_w * 100.0, min_pct), 100.0)

    return int(round(rate_pct * (10 ** abs(sf))))


# ---------------------------------------------------------------------------
# Weather forecast
# ---------------------------------------------------------------------------

def estimate_tomorrow_kwh(
    shortwave_mj_per_m2: float,
    pv_peak_kwp: float,
    performance_ratio: float,
) -> float:
    """Estimate tomorrow's PV yield from Open-Meteo's shortwave radiation sum.

    shortwave_mj_per_m2: daily radiation sum in MJ/m² (from Open-Meteo).
    pv_peak_kwp:         installed peak power in kWp.
    performance_ratio:   system losses factor (typically 0.70–0.80).
    """
    kwh_per_m2 = shortwave_mj_per_m2 / 3.6
    return kwh_per_m2 * pv_peak_kwp * performance_ratio


def _is_weather_cache_valid(cache: dict, cfg: Config, today: datetime.date) -> bool:
    """Return True if the cache is fresh, for the right date, and the right location."""
    try:
        if abs(cache["latitude"] - cfg.latitude) > 0.01:
            return False
        if abs(cache["longitude"] - cfg.longitude) > 0.01:
            return False
        tomorrow = (today + datetime.timedelta(days=1)).isoformat()
        if cache["target_date"] != tomorrow:
            return False
        fetched_at = datetime.datetime.fromisoformat(cache["fetched_at"])
        age_h = (
            datetime.datetime.now(datetime.timezone.utc) - fetched_at
        ).total_seconds() / 3600.0
        return age_h <= cfg.refresh_hours
    except (KeyError, TypeError, ValueError):
        return False


def _read_weather_cache(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_weather_cache(
    path: str,
    lat: float,
    lon: float,
    target_date: str,
    expected_kwh: float,
) -> None:
    cache = {
        "latitude": lat,
        "longitude": lon,
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "target_date": target_date,
        "expected_kwh": expected_kwh,
    }
    with open(path, "w") as f:
        json.dump(cache, f)


def _fetch_open_meteo(lat: float, lon: float, tz_name: str, target_date: str) -> float:
    """Fetch shortwave_radiation_sum for target_date from Open-Meteo.

    Returns expected kWh/m² (already unit-converted) for the target date.
    Raises RuntimeError on network or parse failure.
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=shortwave_radiation_sum"
        f"&timezone={urllib.parse.quote(tz_name)}"
        "&forecast_days=3"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Open-Meteo request failed: {exc}") from exc

    dates: list = data["daily"]["time"]
    radiation: list = data["daily"]["shortwave_radiation_sum"]
    try:
        idx = dates.index(target_date)
        mj = float(radiation[idx])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(
            f"Date {target_date} not in Open-Meteo response (got {dates})"
        ) from exc

    return mj


def get_or_refresh_forecast(cfg: Config, today: datetime.date) -> Optional[float]:
    """Return tomorrow's expected kWh, refreshing the cache when needed.

    Returns None and logs a warning if the fetch fails and no usable cache exists.
    """
    cache = _read_weather_cache(cfg.cache_path)
    if cache and _is_weather_cache_valid(cache, cfg, today):
        expected = cache["expected_kwh"]
        log.info(
            "Weather cache valid — tomorrow's expected production: %.1f kWh", expected
        )
        return expected

    log.info("Weather cache absent or stale — fetching from Open-Meteo")
    tomorrow = (today + datetime.timedelta(days=1)).isoformat()
    try:
        mj = _fetch_open_meteo(cfg.latitude, cfg.longitude, cfg.timezone, tomorrow)
        expected = estimate_tomorrow_kwh(mj, cfg.pv_peak_kwp, cfg.performance_ratio)
        log.info(
            "Forecast %s: %.1f MJ/m² → %.1f kWh (%.1f kWp × PR %.2f)",
            tomorrow, mj, expected, cfg.pv_peak_kwp, cfg.performance_ratio,
        )
        _write_weather_cache(cfg.cache_path, cfg.latitude, cfg.longitude, tomorrow, expected)
        return expected
    except RuntimeError as exc:
        log.warning("Forecast fetch failed: %s — falling back to exporting", exc)
        return None


# ---------------------------------------------------------------------------
# Modbus I/O helpers
# ---------------------------------------------------------------------------

def connect_device(cfg: Config):
    """Connect to the SunSpec device and return a scanned device object."""
    if not cfg.host:
        log.error("No host configured. Set [connection] host in config or --connection.host")
        sys.exit(1)
    log.info("Connecting to %s:%d (slave %d)", cfg.host, cfg.port, cfg.slave_id)
    device = sunspec_client.SunSpecModbusClientDeviceTCP(
        slave_id=cfg.slave_id,
        ipaddr=cfg.host,
        ipport=cfg.port,
    )
    try:
        device.scan()
    except Exception as exc:
        log.error("Device scan failed: %s", exc)
        sys.exit(1)
    return device


def read_storage_model(device):
    """Return the model 124 (Basic Storage Controls) object after reading it."""
    if 124 not in device.models:
        log.error("SunSpec model 124 (Basic Storage Controls) not found — aborting")
        sys.exit(1)
    storage = device.models[124][0]
    try:
        storage.read()
    except Exception as exc:
        log.error("Failed to read storage model 124: %s", exc)
        sys.exit(1)
    return storage


def write_storage_model(storage, inw_rte: int, out_w_rte: Optional[int], storctl_mod: int, dry_run: bool) -> None:
    """Write InWRte, StorCtl_Mod, and optionally OutWRte to the device.

    out_w_rte=None leaves OutWRte at whatever value is already in the model
    (from the preceding read), which is the correct behaviour for charge-only mode.
    """
    storage.InWRte.value = inw_rte
    storage.StorCtl_Mod.value = storctl_mod
    if out_w_rte is not None:
        storage.OutWRte.value = out_w_rte

    if dry_run:
        log.info(
            "Dry-run — would write: InWRte=%d  OutWRte=%s  StorCtl_Mod=%d",
            inw_rte,
            str(out_w_rte) if out_w_rte is not None else "(unchanged)",
            storctl_mod,
        )
        return

    try:
        storage.write()
    except Exception as exc:
        log.error("Failed to write storage model: %s", exc)
        sys.exit(1)

    log.info(
        "Written: InWRte=%d  OutWRte=%s  StorCtl_Mod=%d",
        inw_rte,
        str(out_w_rte) if out_w_rte is not None else "(unchanged)",
        storctl_mod,
    )


# ---------------------------------------------------------------------------
# Subcommand: detect
# ---------------------------------------------------------------------------

def cmd_detect(args) -> None:
    """Query the device and write static info into the config file."""
    cfg = load_config(args.config, _collect_overrides(args))

    device = connect_device(cfg)

    # --- model 1 (Common / identity) ----------------------------------------
    if 1 not in device.models:
        log.error("SunSpec model 1 (Common) not found — cannot read identity")
        sys.exit(1)
    m1 = device.models[1][0]
    manufacturer = str(m1.Mn.value)
    model_name = str(m1.Md.value)
    serial = str(m1.SN.value)
    firmware = str(m1.Vr.value)
    log.info(
        "Device  manufacturer=%r  model=%r  serial=%r  firmware=%r",
        manufacturer, model_name, serial, firmware,
    )

    # --- model 120 (Nameplate / battery energy capacity) --------------------
    capacity_kwh: Optional[float] = None
    wchamax_w: Optional[float] = None

    if 120 in device.models:
        nm = device.models[120][0]
        try:
            nm.read()
            if nm.WHRtg.value is not None:
                sf = nm.WHRtg_SF.value if nm.WHRtg_SF.value is not None else 0
                wh = nm.WHRtg.value * (10 ** sf)
                capacity_kwh = wh / 1000.0
                log.info("Nameplate  WHRtg=%.0f Wh → capacity_kwh=%.2f", wh, capacity_kwh)
        except Exception as exc:
            log.warning("Failed to read model 120 (Nameplate): %s", exc)
    else:
        log.warning("SunSpec model 120 (Nameplate) not found — capacity_kwh not auto-detected")

    # --- model 124 (WChaMax) -------------------------------------------------
    if 124 in device.models:
        storage = read_storage_model(device)
        sf_wcha = storage.WChaMax_SF.value if storage.WChaMax_SF.value is not None else 0
        wchamax_w = storage.WChaMax.value * (10 ** sf_wcha)
        log.info("Storage  WChaMax=%.0f W", wchamax_w)

    # --- write back to config ------------------------------------------------
    updates: dict[str, dict[str, str]] = {
        "detected": {
            "manufacturer": manufacturer,
            "model": model_name,
            "serial": serial,
            "firmware": firmware,
        },
    }
    if capacity_kwh is not None:
        updates.setdefault("battery", {})["capacity_kwh"] = f"{capacity_kwh:.3f}"
    if wchamax_w is not None:
        updates.setdefault("battery", {})["wchamax_w"] = f"{wchamax_w:.0f}"

    if args.dry_run:
        log.info("Dry-run — would update config with: %s", updates)
    else:
        update_config_file(args.config, updates)
        log.info("Config updated: %s", args.config)


# ---------------------------------------------------------------------------
# Subcommand: forecast
# ---------------------------------------------------------------------------

def cmd_forecast(args) -> None:
    """Fetch tomorrow's solar forecast and write/refresh the weather cache."""
    cfg = load_config(args.config, _collect_overrides(args))

    if cfg.latitude is None or cfg.longitude is None or not cfg.timezone:
        log.error(
            "forecast requires [location] latitude, longitude, and timezone to be set"
        )
        sys.exit(1)
    if cfg.pv_peak_kwp <= 0:
        log.error("forecast requires [weather] pv_peak_kwp > 0")
        sys.exit(1)

    today = datetime.date.today()
    tomorrow = (today + datetime.timedelta(days=1)).isoformat()
    try:
        mj = _fetch_open_meteo(cfg.latitude, cfg.longitude, cfg.timezone, tomorrow)
    except RuntimeError as exc:
        log.error("%s", exc)
        sys.exit(1)

    expected = estimate_tomorrow_kwh(mj, cfg.pv_peak_kwp, cfg.performance_ratio)
    log.info(
        "Forecast %s: %.1f MJ/m² → %.1f kWh (%.1f kWp × PR %.2f)",
        tomorrow, mj, expected, cfg.pv_peak_kwp, cfg.performance_ratio,
    )

    if args.dry_run:
        log.info("Dry-run — would write cache to %s", cfg.cache_path)
        return

    _write_weather_cache(cfg.cache_path, cfg.latitude, cfg.longitude, tomorrow, expected)
    log.info("Cache written: %s", cfg.cache_path)


# ---------------------------------------------------------------------------
# Subcommand: run
# ---------------------------------------------------------------------------

def cmd_run(args) -> None:
    """Main control loop — determine phase and apply charge/discharge setpoints."""
    cfg = load_config(args.config, _collect_overrides(args))

    # Validate feature flag compatibility before touching the device
    try:
        validate_features(cfg)
    except ValueError as exc:
        log.error("Configuration error: %s", exc)
        sys.exit(1)

    log.info(
        "Features: charge_limit=%s  end_of_day=%s  night_export=%s  weather=%s",
        cfg.feat_charge_limit,
        cfg.feat_end_of_day_full_charge,
        cfg.feat_night_grid_export,
        cfg.feat_weather_aware,
    )

    # --- connect and read model 124 -----------------------------------------
    device = connect_device(cfg)

    if 1 in device.models:
        m1 = device.models[1][0]
        log.info(
            "Device  manufacturer=%r  model=%r  serial=%r  firmware=%r",
            m1.Mn.value, m1.Md.value, m1.SN.value, m1.Vr.value,
        )

    storage = read_storage_model(device)
    sf: int = storage.InOutWRte_SF.value
    sf_abs = abs(sf)
    max_raw = int(100 * (10 ** sf_abs))

    soc: float = storage.ChaState.value * (10 ** storage.ChaState_SF.value)
    wchamax: float = storage.WChaMax.value * (10 ** storage.WChaMax_SF.value)
    log.info("Battery  SoC=%.2f%%  WChaMax=%.0f W  InOutWRte_SF=%d", soc, wchamax, sf)

    if wchamax == 0:
        log.warning("WChaMax=0 — is a battery connected? Aborting.")
        return

    # --- action shortcuts (restore / deactivate) ----------------------------
    if args.deactivate:
        log.info("--deactivate: setting StorCtl_Mod=0")
        write_storage_model(storage, max_raw, max_raw, 0, args.dry_run)
        if not args.restore:
            return

    if args.restore:
        log.info("--restore: resetting InWRte and OutWRte to 100%%, StorCtl_Mod=0")
        write_storage_model(storage, max_raw, max_raw, 0, args.dry_run)
        return

    # --- determine current phase --------------------------------------------
    # When location features are not enabled we skip sun time computation.
    now: datetime.datetime
    phase: str

    if (
        cfg.feat_end_of_day_full_charge
        or cfg.feat_night_grid_export
        or cfg.feat_charge_limit
    ):
        tz = ZoneInfo(cfg.timezone) if cfg.timezone else None

        if tz and cfg.latitude is not None and cfg.longitude is not None:
            now = datetime.datetime.now(tz=tz)
            today = now.date()
            today_sun = get_sun_times(cfg.latitude, cfg.longitude, cfg.timezone, today)
            sunrise = today_sun["sunrise"]
            sunset = today_sun["sunset"]
            phase = determine_phase(now, sunrise, sunset, cfg.lead_time_minutes)
            log.info(
                "Phase: %s  now=%s  sunrise=%s  sunset=%s",
                phase,
                now.strftime("%H:%M"),
                sunrise.strftime("%H:%M"),
                sunset.strftime("%H:%M"),
            )
        else:
            # No location configured — only day-time features can work
            now = datetime.datetime.now()
            today = now.date()
            phase = PHASE_DAY
    else:
        now = datetime.datetime.now()
        today = now.date()
        phase = PHASE_DAY

    # --- apply control logic per phase --------------------------------------

    if phase == PHASE_NIGHT and cfg.feat_night_grid_export:
        _apply_night_export(args, cfg, storage, soc, wchamax, sf, sf_abs, max_raw, now, today)
        return

    if phase == PHASE_END_OF_DAY and cfg.feat_end_of_day_full_charge:
        log.info("End-of-day: lifting all charge limits (charging to 100%%)")
        write_storage_model(storage, max_raw, max_raw, 0, args.dry_run)
        return

    # DAY (or night/end_of_day with features disabled) → apply charge limiting
    _apply_charge_control(args, cfg, storage, soc, wchamax, sf, sf_abs, max_raw)


def _apply_charge_control(args, cfg, storage, soc, wchamax, sf, sf_abs, max_raw):
    """Apply taper + optional upper-limit charge control."""
    if not cfg.feat_charge_limit and not cfg.charge_taper:
        log.info("No charge_limit feature and no taper rules — nothing to do")
        return

    inw_rte, storctl = compute_charge_setpoint(
        soc=soc,
        taper=cfg.charge_taper,
        upper_soc_limit=cfg.upper_soc_limit,
        min_charge_rate_w=cfg.min_charge_rate_w,
        wchamax_w=wchamax,
        sf=sf,
        feat_charge_limit=cfg.feat_charge_limit,
    )

    if storctl == 0:
        log.info(
            "SoC %.2f%% below all thresholds — full charge rate (StorCtl_Mod=0)", soc
        )
    elif inw_rte == 0:
        log.info(
            "SoC %.2f%% >= upper limit %.1f%% — stopping charge (InWRte=0 StorCtl_Mod=1)",
            soc, cfg.upper_soc_limit,
        )
    else:
        rate_pct = inw_rte / (10 ** sf_abs)
        log.info(
            "SoC %.2f%% — charge rate limited to %.2f%% (raw %d, StorCtl_Mod=1)",
            soc, rate_pct, inw_rte,
        )

    write_storage_model(storage, inw_rte, None, storctl, args.dry_run)


def _apply_night_export(args, cfg, storage, soc, wchamax, sf, sf_abs, max_raw, now, today):
    """Apply controlled discharge for night grid export."""
    # Weather check
    if cfg.feat_weather_aware:
        expected_kwh = get_or_refresh_forecast(cfg, today)
        if expected_kwh is not None and expected_kwh < cfg.skip_export_below_kwh:
            log.info(
                "Tomorrow's forecast %.1f kWh < threshold %.1f kWh — skipping grid export",
                expected_kwh, cfg.skip_export_below_kwh,
            )
            write_storage_model(storage, max_raw, max_raw, 0, args.dry_run)
            return
        if expected_kwh is None:
            log.info("No forecast available — proceeding with export (fail-open)")

    # Reserve check
    if soc <= cfg.reserve_soc:
        log.info(
            "SoC %.2f%% at/below reserve %.1f%% — stopping discharge",
            soc, cfg.reserve_soc,
        )
        write_storage_model(storage, max_raw, max_raw, 0, args.dry_run)
        return

    # Compute hours until next sunrise
    tz = ZoneInfo(cfg.timezone)
    tomorrow = today + datetime.timedelta(days=1)
    next_sun = get_sun_times(cfg.latitude, cfg.longitude, cfg.timezone, tomorrow)
    next_sunrise = next_sun["sunrise"]
    # Handle the case where we're before today's sunrise (still last night)
    if now >= next_sunrise:
        # Extremely rare edge case: sunrise is imminent
        log.info("Sunrise imminent — stopping discharge")
        write_storage_model(storage, max_raw, max_raw, 0, args.dry_run)
        return

    hours_left = (next_sunrise - now).total_seconds() / 3600.0

    out_raw = compute_discharge_setpoint(
        soc=soc,
        reserve_soc=cfg.reserve_soc,
        capacity_kwh=cfg.capacity_kwh,
        hours_left=hours_left,
        wchamax_w=wchamax,
        min_discharge_w=cfg.min_discharge_rate_w,
        sf=sf,
    )

    if out_raw is None:
        log.info("Discharge setpoint: nothing to discharge — restoring defaults")
        write_storage_model(storage, max_raw, max_raw, 0, args.dry_run)
        return

    energy_left = (soc - cfg.reserve_soc) / 100.0 * cfg.capacity_kwh
    required_w = energy_left * 1000.0 / hours_left
    rate_pct = out_raw / (10 ** sf_abs)
    log.info(
        "Night export: SoC %.2f%% → reserve %.1f%%  energy=%.2f kWh  "
        "hours_left=%.2fh  required=%.0f W  OutWRte=%.2f%% (raw %d)  StorCtl_Mod=1",
        soc, cfg.reserve_soc, energy_left, hours_left, required_w, rate_pct, out_raw,
    )
    # StorCtl_Mod=1: discharge limit NOT active, charge limit active
    # don't get confused, setting a _charge_ limit with a negative value
    # actually means discharge with at least out_raw Watt.
    write_storage_model(storage, -1 * out_raw, max_raw, 1, args.dry_run)


# ---------------------------------------------------------------------------
# Argument parsing helpers
# ---------------------------------------------------------------------------

def _collect_overrides(args: argparse.Namespace) -> dict:
    """Extract 'section.key' → value pairs from the parsed namespace."""
    return {
        k: v
        for k, v in vars(args).items()
        if "." in k and v is not None
    }


def _add_config_overrides(parser: argparse.ArgumentParser) -> None:
    """Add --section.key override flags for commonly-tweaked config values."""
    overrides = [
        ("connection.host",              str,   "SunSpec device hostname or IP"),
        ("connection.port",              int,   "Modbus TCP port"),
        ("connection.slave_id",          int,   "Modbus slave ID"),
        ("charging.upper_soc_limit",     float, "Upper SOC limit %% (charge_limit)"),
        ("charging.min_charge_rate_w",   float, "Minimum charge rate in W"),
        ("grid_export.reserve_soc",      float, "Sunrise SOC reserve %%"),
        ("weather.cache_path",           str,   "Path to weather cache JSON file"),
        ("battery.capacity_kwh",         float, "Battery capacity in kWh"),
    ]
    for dotkey, typ, help_text in overrides:
        parser.add_argument(
            f"--{dotkey}",
            dest=dotkey,
            type=typ,
            default=None,
            metavar=dotkey.split(".")[1].upper(),
            help=f"{help_text} (overrides config file)",
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Battery charge/discharge control for Fronius Gen24 via SunSpec Modbus TCP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default="batctl.conf",
        metavar="FILE",
        help="Path to the INI config file (default: batctl.conf)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read state and log intended action but do not write anything",
    )

    sub = parser.add_subparsers(dest="subcommand")
    sub.required = False  # 'run' is the default

    # --- run ----------------------------------------------------------------
    run_p = sub.add_parser("run", help="Control charge/discharge (default subcommand)")
    run_p.add_argument(
        "--restore",
        action="store_true",
        help="Reset InWRte/OutWRte to 100%% and StorCtl_Mod to 0, then exit",
    )
    run_p.add_argument(
        "--deactivate",
        action="store_true",
        help="Set StorCtl_Mod=0 to disable the charge/discharge limit, then exit",
    )
    _add_config_overrides(run_p)

    # --- detect -------------------------------------------------------------
    detect_p = sub.add_parser(
        "detect",
        help="Read device identity and battery capacity; write into config file",
    )
    _add_config_overrides(detect_p)

    # --- forecast -----------------------------------------------------------
    forecast_p = sub.add_parser(
        "forecast",
        help="Fetch tomorrow's solar forecast and refresh the weather cache",
    )
    _add_config_overrides(forecast_p)

    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    # Apply --dry-run to the parsed namespace so subcommands can access it
    if not hasattr(args, "restore"):
        args.restore = False
    if not hasattr(args, "deactivate"):
        args.deactivate = False

    subcommand = args.subcommand or "run"

    if subcommand == "detect":
        cmd_detect(args)
    elif subcommand == "forecast":
        cmd_forecast(args)
    else:
        cmd_run(args)


if __name__ == "__main__":
    main()
