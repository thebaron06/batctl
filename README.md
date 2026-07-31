# batctl

Battery charge and discharge controller for the **Fronius Gen24 / Primo / Symo** inverter series with an attached storage battery (e.g. BYD HVS).

batctl runs as a periodic cron or systemd job (every 5 minutes by default) and communicates with the inverter over **Modbus TCP** using the **SunSpec** protocol. It covers four distinct modes of operation, all individually configurable and guarded by feature flags.

---

## What it does

### Charge taper (always active when rules are configured)

Throttles the battery charge rate as the state of charge (SoC) rises, following a set of configurable threshold → scale-factor rules. This protects battery longevity near full charge and keeps export capacity available when PV production is high.

### Charge upper limit (`charge_limit`)

Caps the battery SoC at a configurable level during the day (e.g. 80 %). Energy above the cap is exported to the grid or covers house loads instead of being stored. This makes room in the battery for arbitrage at end of day.

### End-of-day full charge (`end_of_day_full_charge`)

In a configurable window before sunset (default: 2 hours), the SoC cap is lifted so the battery charges to 100 %. Requires `charge_limit = true` and location settings so sunset time can be computed.

### Night grid export (`night_grid_export`)

Between sunset and sunrise the controller calculates a discharge rate that drains the battery evenly down to a configurable sunrise reserve (default: 12 %). The setpoint is recomputed at every script invokation from the current SoC and remaining time to sunrise, so it self-corrects if house loads change during the night.

The sunrise reserve is intended to cover morning consumption (e.g. breakfast) until solar production is sufficient to power the home and recharge the battery.

> **Important:** whether the discharged energy actually flows to the grid or only covers house loads depends on your inverter's feed-in configuration, not on batctl. Verify with your smart meter after the first night run.

### Time-window charging (`charge_windows`)

Restricts charging to one or more configurable time windows during the day. Outside any defined window, charging is throttled to `min_charge_rate_w` — set that to `0` to block charging completely.

Typical use: allow full charging only around solar noon (e.g. 10:00–15:00) so the battery fills when PV production peaks, avoiding charging from the grid in the early morning or late afternoon.

Multiple windows can be defined; if the current time falls inside *any* window, normal charging logic applies. Windows may cross midnight (e.g. `22:00-06:00`).

The feature is independent of `charge_limit` but compatible with it: within a window, taper and upper-limit rules still apply; outside a window, the min rate applies regardless of SoC. Requires `[location] timezone` for local time.

### Weather-aware export (`weather_aware`)

If tomorrow's solar production forecast is below a configurable threshold, the night export is skipped and the battery is kept for next-day home use. Uses [Open-Meteo](https://open-meteo.com) (free, no API key needed). If the forecast fetch fails, batctl falls back to exporting (fail-open).

---

## Requirements

- Python 3.11+
- `pysunspec2`
- `astral`
- A Fronius Gen24 / Primo / Symo inverter with battery storage reachable over Modbus TCP
- `pyserial` (pulled in by pysunspec2)

Install:
```
pip install -r requirements.txt
```

---

## Quick start

**1. Copy and edit the config:**
```sh
cp batctl.conf.example batctl.conf   # or edit batctl.conf directly
# Set [connection] host to your inverter's hostname or IP
```

**2. Auto-detect battery capacity and identity:**
```sh
python batctl.py detect --config batctl.conf
```
This reads the inverter nameplate via SunSpec and writes `[battery]` and `[detected]` into the config file. Re-run whenever the hardware changes.

**3. Set your location** in `[location]`:
```ini
latitude  = 48.21
longitude = 16.37
timezone  = Europe/Vienna
```
Sunrise and sunset times are computed locally from this data — no network needed.

**4. Enable features one at a time and test with `--dry-run`:**
```sh
python batctl.py run --config batctl.conf --dry-run
```
No writes are made in dry-run mode; the intended setpoints are logged.

**5. Schedule with cron or systemd** (see `scripts/`).

---

## Subcommands

### `run` (default)

Reads the inverter state, determines the current phase, and applies the appropriate charge or discharge setpoint.

```
python batctl.py run --config batctl.conf [--dry-run] [--restore] [--deactivate]
```

| Flag | Effect |
|---|---|
| `--dry-run` | Log intended action, write nothing |
| `--restore` | Reset `InWRte`/`OutWRte` to 100 % and `StorCtl_Mod` to 0, then exit |
| `--deactivate` | Set `StorCtl_Mod = 0` (disable all limits), then exit |

### `detect`

Queries the inverter once and writes static values (battery capacity, device identity) into the config file. Re-run if you change the battery or inverter.

```
python batctl.py detect --config batctl.conf [--dry-run]
```

### `forecast`

Fetches tomorrow's solar production forecast from Open-Meteo and writes a local cache file. Called automatically by `run` when the cache is absent or stale. Can also be scheduled independently (e.g. a few times per day).

```
python batctl.py forecast --config batctl.conf [--dry-run]
```

---

## Command-line overrides

Any `[section] key` from the config file can be overridden on the command line using `--section.key VALUE`. CLI values take precedence over the file. Useful for testing or multi-inverter setups:

```sh
python batctl.py run --config batctl.conf --connection.host 192.168.1.100 --dry-run
python batctl.py run --config batctl.conf --grid_export.reserve_soc 20
python batctl.py run --config batctl.conf --weather.cache_path /run/batctl_cache.json
```

---

## Configuration reference

Full annotated config with all options and defaults: see `batctl.conf`.

Key settings:

| Section | Key | Default | Description |
|---|---|---|---|
| `connection` | `host` | *(required)* | Inverter hostname or IP |
| `features` | `charge_limit` | `false` | Enable SoC upper cap |
| `features` | `end_of_day_full_charge` | `false` | Lift cap near sunset |
| `features` | `night_grid_export` | `false` | Discharge to grid overnight |
| `features` | `weather_aware` | `false` | Skip export on cloudy forecast |
| `features` | `charge_windows` | `false` | Restrict charging to time windows |
| `charging` | `upper_soc_limit` | `80` | Day-time SoC cap (%) |
| `grid_export` | `reserve_soc` | `12` | Minimum SoC at sunrise (%) |
| `end_of_day` | `lead_time_minutes` | `120` | Minutes before sunset to lift cap |
| `battery` | `capacity_kwh` | auto via `detect` | Battery energy capacity (kWh) |
| `weather` | `pv_peak_kwp` | *(required for weather_aware)* | Installed PV peak power (kWp) |
| `weather` | `skip_export_below_kwh` | `10` | Forecast threshold to skip export |
| `weather` | `cache_path` | `/tmp/batctl_weather_cache.json` | Weather cache location |

### Feature dependencies (enforced at startup)

```
end_of_day_full_charge  →  charge_limit AND [location]
night_grid_export       →  [location] AND [battery] capacity_kwh > 0
weather_aware           →  night_grid_export AND [weather] pv_peak_kwp > 0
charge_windows          →  [location] timezone AND at least one entry in [charge_windows]
```

A misconfigured combination exits immediately with a clear error message.

---

## Weather cache

The forecast is cached in `[weather] cache_path` (default `/tmp/batctl_weather_cache.json`) so that the network is only hit a few times per day rather than every cron run. The cache is automatically invalidated when:

- It is older than `[weather] refresh_hours` (default 6 hours)
- It is for the wrong date
- The configured location has changed

To force a refresh, delete the file or run:
```sh
python batctl.py forecast --config batctl.conf
```

If you change the `[location]` settings, the old cache will be ignored automatically on the next run.

---

## Scheduling

### cron

See `scripts/cron/` for a ready-to-use wrapper script and installation instructions.

```
*/5 * * * *  /path/to/batctl/scripts/cron/cron-wrapper-batctl.sh
```

### systemd

See `scripts/systemd/` for a service + timer unit and installation instructions.

---

## Limitations

- **Grid export is inverter-dependent.** Setting a discharge rate via Modbus limits how fast the battery discharges. Whether surplus power flows to the grid depends on your inverter's feed-in settings and local grid regulations, not on batctl. Verify with a smart meter.
- **Charge taper rules are for battery health only.** At end-of-day the taper is deliberately lifted to allow full charging.
- **No real-time adjustment for unexpected production or consumption.** The discharge rate is recomputed each cron run from current SoC and time remaining, which self-corrects gradually. Large unexpected events (e.g. storm knocking out PV for hours) are handled on the next cycle.
- **Weather model is simplified.** Expected production = `irradiance × peak_kwp × performance_ratio`. Shading, temperature de-rating, and seasonal tilt variations are not modelled.

## Good to know

Don't let yourself confuse by the register wording (Dis-/Chargelimit). It might work different than you think, at least for Fronius inverters.
Setting a discharge limit (`OutWRte`) and enable that in the Bitmask (`StorCtl_Mod`) set to `2` does **not** mean that the battery is forced to discharge.
It just means, that the discharging limit is set to the value of `OutWRte`.

To force the inverter to discharge, but do not limit how much power can be taken from the battery at all, a negative charge limit must be set and activated.
An example, your inverter's `WChaMax` is `7680`W (already scaled) and you set `InWRte` to `-727`, `OutWRte` to `10000` and `StorCtl_Mod` to `1`, the battery will
discharge with at least `727`W, but it is not limited to that discharge rate. Usually that is what you want when you want to feed-back the energy from the battery
to the grid over night.
