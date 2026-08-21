# batctl

Battery charge and discharge controller for the **Fronius Gen24 / Primo / Symo** inverter series with an attached storage battery (e.g. BYD HVS).

batctl runs as a periodic cron or systemd job (eg. every 5 minutes) and communicates with the inverter over **Modbus TCP** using the **SunSpec** protocol. All modes of operation are individually configurable and guarded by feature flags.

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

### Weather-aware export (`weather_aware`)

If tomorrow's solar production forecast is below a configurable threshold, the night export is skipped and the battery is kept for next-day home use. Uses [Open-Meteo](https://open-meteo.com) (free, no API key needed). If the forecast fetch fails, batctl falls back to exporting (fail-open).

### Discharge horizon extension (`extend_hours`)

Softens the overnight discharge curve by pretending the battery has more time to drain than it actually does. The discharge rate is calculated against `hours_until_sunrise + extend_hours`, so the battery arrives at sunrise with more charge remaining to cover morning household loads while solar ramps up.

Set `extend_hours` to a fixed number of hours or to `auto`. In auto mode batctl reads tomorrow's hourly forecast and computes how many hours after sunrise PV output is expected to exceed `avg_base_load_w`, then uses that as the extension.

### Charge windows (`charge_windows`)

Restrict battery charging to named time windows. Outside all configured windows, charging is throttled to `min_charge_rate_w` (set it to `0` to block charging entirely). Midnight-crossing windows (e.g. `22:00-06:00`) are supported. Useful for keeping charging capacity free in the afternoon so the end-of-day top-up has headroom, or for avoiding grid draw during expensive tariff periods.

```ini
[features]
charge_windows = true

[charge_windows]
midday = 10:00-15:00
```

### Charging profiles (`profile`)

Automatically shapes the charge rate ceiling across the day based on the hourly solar forecast. The plan is generated once per day from the cached forecast and stored in the state file.

| Profile | Behaviour |
|---|---|
| `solar_follow` | Charge rate proportional to forecast power at each hour. Peaks at solar noon, low in early morning and late afternoon. Naturally concentrates charging around midday. |
| `ramp_up` | Proportional to solar through the morning ascent, then holds at 100 % after the daily peak. Battery charges gently while solar rises, then as fast as possible in the afternoon. |
| `spread` | Uniform 100 % ceiling across all daylight hours. Acts as a time gate (sunrise to sunset only) without adding a rate ceiling; charge taper and `charge_limit` still govern the actual rate. |

The profile ceiling is applied in addition to existing taper rules and the `charge_limit` cap. Requires `[weather] pv_peak_kwp > 0` and `[location] timezone`.

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
| `charging` | `avg_base_load_w` | `200` | Average household base load (W); added to night discharge rate |
| `charging` | `profile` | `none` | Charging profile: `none`, `solar_follow`, `spread`, `ramp_up` |
| `grid_export` | `reserve_soc` | `12` | Minimum SoC at sunrise (%) |
| `grid_export` | `extend_hours` | `0` | Extra hours added to discharge horizon (`0`, float, or `auto`) |
| `end_of_day` | `lead_time_minutes` | `120` | Minutes before sunset to lift cap |
| `battery` | `capacity_kwh` | auto via `detect` | Battery energy capacity (kWh) |
| `weather` | `pv_peak_kwp` | *(required for weather_aware/profile)* | Installed PV peak power (kWp) |
| `weather` | `skip_export_below_kwh` | `10` | Forecast threshold to skip export |
| `weather` | `cache_path` | `/tmp/batctl_weather_cache.json` | Weather cache location |
| `statefile` | `enabled` | `false` | Enable state file to reduce Modbus writes |
| `statefile` | `path` | `/tmp/batctl_state.json` | State file location |

### Feature dependencies (enforced at startup)

```
end_of_day_full_charge  ->  charge_limit AND [location]
night_grid_export       ->  [location] AND [battery] capacity_kwh > 0
weather_aware           ->  night_grid_export AND [weather] pv_peak_kwp > 0
charge_windows          ->  [location] timezone AND at least one window in [charge_windows]
charging profile        ->  [location] timezone AND [weather] pv_peak_kwp > 0
extend_hours = auto     ->  [weather] pv_peak_kwp > 0 (uses hourly forecast)
```

A misconfigured combination exits immediately with a clear error message.

---

## Weather cache

The forecast is cached in `[weather] cache_path` (default `/tmp/batctl_weather_cache.json`) so that the network is only hit a few times per day rather than every cron run. The cache stores:

- **Daily radiation sum** (MJ/m^2) for tomorrow -- used by `weather_aware` to decide whether to skip export
- **Hourly radiation** (W/m^2) for today and tomorrow -- used by charging profiles and `extend_hours = auto`

The cache is automatically invalidated when:

- It is older than `[weather] refresh_hours` (default 6 hours)
- It is for the wrong date
- The configured location has changed

To force a refresh, delete the file or run:
```sh
python batctl.py forecast --config batctl.conf
```

If you change the `[location]` settings, the old cache will be ignored automatically on the next run.

---

## State file

When `[statefile] enabled = true`, batctl writes the last-applied inverter setpoint (StorCtl_Mod, InWRte, OutWRte) to a JSON file after each successful Modbus write. On subsequent cron runs it skips the write entirely if:

- The computed setpoint is identical to the last-written one, **and**
- The last write was within the current clock-hour

This reduces the number of Modbus round-trips from one every 5 minutes to at most one per hour under stable conditions (e.g. during a long night export at a fixed rate).

The state file also caches the daily charging plan so it is not regenerated on every cron run.

```ini
[statefile]
enabled = true
path    = /tmp/batctl_state.json
```

State file writes are always skipped in `--dry-run` mode.

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

## Recommended taper values for BYD HVS (LFP)

The BYD HVS series uses **LFP (LiFePO4) chemistry**. Its voltage curve is nearly flat from 20–80 % SoC and rises steeply above 80 %. Tapering below 80 % has negligible benefit; the region above 80 % is where slowing the charge rate protects cell longevity and allows the BMS to balance cells properly.

### `[charge_taper]` — day phase (with `upper_soc_limit = 80`)

When the day cap is at 80 %, thresholds above 80 % are never reached. A short taper approaching the cap is enough:

```ini
[charge_taper]
78 = 0.50   # half rate in the last 2 % before the cap
75 = 0.80   # light taper at 75 % (optional)
```

### `[eod_charge_taper]` — end-of-day full charge (charging to 100 %)

This is where taper matters most. The battery charges through the steep voltage region:

```ini
[eod_charge_taper]
80 = 0.70   # 70 % rate — voltage begins rising
85 = 0.40   # 40 % — steeper region
90 = 0.20   # 20 % — absorption zone begins
95 = 0.08   # ~8 % — tail charge, BMS handles final balancing
```

On a 5 kW `WChaMax` this maps to roughly 3.5 kW → 2.0 kW → 1.0 kW → 400 W as the battery fills. The last step will not go below `min_charge_rate_w`.

### Notes

- **The biggest longevity win** is the day cap at 80 %, not the taper itself. Only reaching 100 % once a day (end-of-day) rather than every cycle can significantly extend cell life.
- **Temperature derating:** LFP should be charged at reduced rates below 10 °C. The BMS handles this internally; no batctl change needed.
- **The BMS governs the final 2–3 %:** above roughly 95 % the BMS switches to constant-voltage mode regardless of the setpoint. The `95 = 0.08` entry just limits the ceiling for that phase.

### Sources

- [BYD Battery-Box Premium HVS 7.7 datasheet](https://www.offgridtec.com/en/byd-hvs-7.7-battery-box-premium-7.68-kwh-307.2v-lifepo4-storage-system.html) — chemistry confirmation, cycle rating, charge specs
- [Battery University — How to prolong lithium-based batteries](https://www.batteryuniversity.com/article/bu-808-how-to-prolong-lithium-based-batteries/) — authoritative reference on SoC windows and cycle life
- [LiFePO4 voltage / SoC chart (Wevolver)](https://www.wevolver.com/article/lifepo4-voltage-chart-soc-voltages-for-32v12v24v48v-systems) — per-cell voltage at each SoC level
- [LFP charging phases and voltage curves (Deespaek)](https://www.deespaek.com/what-are-the-charging-phases-and-voltage-curves-of-lfp-batteries/) — CC/CV transition and taper rationale
- [DIY Solar Forum — Max safe charge rate for LFP](https://diysolarforum.com/threads/max-safe-charge-rate-for-lfp.109998/) — installer experience and community consensus
- [Fronius Symo GEN24 operating manual (Manualslib, p. 116)](https://www.manualslib.com/manual/3962439/Fronius-Symo-Gen24-6-0.html?page=116) — inverter charge configuration options

---

## Good to know

Don't let yourself confuse by the register wording (Dis-/Chargelimit). It might work different than you think, at least for Fronius inverters.
Setting a discharge limit (`OutWRte`) and enable that in the Bitmask (`StorCtl_Mod`) set to `2` does **not** mean that the battery is forced to discharge.
It just means, that the discharging limit is set to the value of `OutWRte`.

To force the inverter to discharge, but do not limit how much power can be taken from the battery at all, a negative charge limit must be set and activated.
An example, your inverter's `WChaMax` is `7680`W (already scaled) and you set `InWRte` to `-727`, `OutWRte` to `10000` and `StorCtl_Mod` to `1`, the battery will
discharge with at least `727`W, but it is not limited to that discharge rate. Usually that is what you want when you want to feed-back the energy from the battery
to the grid over night.
