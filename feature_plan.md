# Future Feature Ideas

Collected during initial implementation — not scheduled, not prioritised.

---

## Smart-meter integration

Read the smart meter (SunSpec model 20x, exposed by the Fronius inverter on a
different Modbus address) to confirm that forced discharge is actually being
exported to the grid rather than only covering house loads. Possible uses:

- Log actual grid feed per run alongside the discharge setpoint.
- Warn if no grid feed is detected despite active discharge (indicates the
  inverter's export settings are blocking it).
- Gate discharge on confirmed export (opt-in; more conservative, avoids pointless
  battery cycling).

---

## Weather: raise-reserve mode

Instead of skipping grid export entirely on a poor forecast day, offer a second
bad-forecast behaviour: keep exporting but raise the sunrise reserve proportional
to the shortfall. Example: if tomorrow is forecast to produce only 50 % of the
threshold, keep 50 % more SoC (raise reserve from 12 % to 18 %).

Config sketch:
```ini
[features]
weather_aware = true
bad_forecast_behavior = raise_reserve   # or: skip_export (current default)
```

---

## Dynamic reserve based on expected morning consumption

Allow the sunrise reserve to be expressed in kWh rather than SoC %, and compute
the required SoC automatically from the detected battery capacity. Would make the
reserve independent of battery size.

---

## Multi-inverter support

Run batctl against several inverters (e.g. a main inverter + a second string
inverter) from the same config with per-device overrides.
