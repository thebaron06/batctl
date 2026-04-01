#!/usr/bin/env python3
"""
Battery charging control via SunSpec (Modbus TCP).

Reads the current state of charge from a SunSpec hybrid inverter/storage
device and limits the maximum charge rate according to configurable
thresholds.

SunSpec models used:
  1   – Common (identification logging)
  124 – Basic Storage Controls

Intended to be called periodically via cron, e.g.:
    */5 * * * *  /path/to/venv/bin/python /path/to/batctl.py \
                     --host 192.168.1.100 \
                     --config /etc/battery/charge_rules.conf
"""

# modbus-register docs: http://www.fronius.com/QR-link/0024
#
# Consider storage.ChaSt
# value description
#    1: OFF          Energiespeicher ist nicht verfügbar
#    2: EMPTY        Energiespeicher ist derzeit vollständig entladen
#    3: DISCHARGING  Energiespeicher wird derzeit entladen
#    4: CHARGING     Energiespeicher wird derzeit geladen
#    5: FULL         Energiespeicher ist derzeit vollständig geladen
#    6: HOLDING      Energiespeicher wird derzeit weder geladen noch entladen
#    7: TESTING      wird während Kalibrations- oder Service-Ladung benutzt
#    The status TESTING is used during battery calibration or service charge.
#
# StorCtl_Mod	Activate hold/discharge/charge storage control mode. Bitfield value.
#               Additional Fronius description: Active hold/discharge/charge storage control mode.
#               Set the charge field to enable charging and the discharge field to enable discharging.
#       bit 0: CHARGE bit 1: DiSCHARGE 
#
# 40356	40356	1	RW	0x03 0x06 0x10	OutWRte	Percent of max discharge rate.  Additional Fronius description: Defines maximum Discharge rate. If not used than the default is 100 and WChaMax defines max. Discharge rate. See WChaMax for details 	int16	% WChaMax	InOutWRte_SF	valid range -100.00% - +100.00%  Please note that this register has a scale factor in Register InOutWRte_SF, so for InOutWRte_SF = -2 the valid range in raw values is from -10000 to 10000.  Please be aware that setting an invalid power window will result in a modbus exception 3 (ILLEGAL DATA VALUE). Invalid power windows are all windows where condition: ((StorCtl_Mod == 3) AND ((-1) * InWRtg > OutWRtg)) evaluates to true. This can happen for example if two negative values are written into InWRtg and OutWRtg and both limits are activated by StorCtl_Mod = 3. 
# 40357	40357	1	RW	0x03 0x06 0x10	InWRte	Percent of max charging rate.  Additional Fronius description: Defines maximum Charge rate. If not used than the default is 100 and WChaMax defines max. Charge rate. See WChaMax for details 	int16	 % WChaMax	InOutWRte_SF	valid range -100.00% - +100.00%  Please note that this register has a scale factor in Register InOutWRte_SF, so for InOutWRte_SF = -2 the valid range in raw values is from -10000 to 10000.  Please be aware that setting an invalid power window will result in a modbus exception 3 (ILLEGAL DATA VALUE). Invalid power windows are all windows where condition: ((StorCtl_Mod == 3) AND ((-1) * InWRtg > OutWRtg)) evaluates to true. This can happen for example if two negative values are written into InWRtg and OutWRtg and both limits are activated by StorCtl_Mod = 3. 
#
#  https://manuals.fronius.com/html/4204102649/de.html#0_m_0000028177


import argparse
import logging
import sys

import sunspec2.modbus.client as sunspec_client

# ---------------------------------------------------------------------------
# Logging – keep format cron-friendly (timestamp always included)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_rules(path: str) -> list[tuple[float, float]]:
    """Load SOC threshold rules from a plain-text config file.

    Each non-comment line must contain exactly two fields:
        <soc_threshold_%>  <scale_factor>

    Returns a list of (threshold, scale) tuples sorted by threshold
    descending so the highest threshold is evaluated first.
    """
    rules: list[tuple[float, float]] = []

    with open(path) as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 2:
                log.warning("config %s:%d – expected 'threshold scale', got %r – skipped",
                            path, lineno, line)
                continue
            try:
                threshold = float(parts[0])
                scale = float(parts[1])
            except ValueError:
                log.warning("config %s:%d – cannot parse numbers from %r – skipped",
                            path, lineno, line)
                continue
            if not (0.0 <= threshold <= 100.0):
                log.warning("config %s:%d – threshold %.1f not in [0, 100] – skipped",
                            path, lineno, threshold)
                continue
            if not (0.0 <= scale <= 1.0):
                log.warning("config %s:%d – scale %.3f not in [0.0, 1.0] – skipped",
                            path, lineno, scale)
                continue
            rules.append((threshold, scale))

    rules.sort(key=lambda t: t[0], reverse=True)
    return rules


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Limit battery charge rate based on state of charge thresholds."
    )
    parser.add_argument("--host", required=True,
                        help="SunSpec device hostname or IP address")
    parser.add_argument("--port", type=int, default=502,
                        help="Modbus TCP port (default: 502)")
    parser.add_argument("--slave-id", type=int, default=1, dest="slave_id",
                        help="Modbus slave ID (default: 1)")
    parser.add_argument("--config", required=True,
                        help="Path to the SOC/scale rules config file")
    parser.add_argument("--min-charge-rate", type=float, default=300.0,
                        dest="min_charge_rate",
                        help="Lower bound for the computed charge rate in Watts (default: 300).")
    parser.add_argument("--restore", action="store_true",
                        help="Unconditionally restore charge limit and exit.")
    parser.add_argument("--deactivate", action="store_true",
                        help="Set StorCtl_Mod=0 to disable the charge limit and exit.")
    parser.add_argument("--dry-run", action="store_true",

                        help="Read state and log intended action, but do not write anything")
    args = parser.parse_args()

    # --- load rules ---------------------------------------------------------
    try:
        rules = load_rules(args.config)
    except OSError as exc:
        log.error("Cannot read config file: %s", exc)
        sys.exit(1)

    if not rules:
        log.error("No valid rules found in %s – nothing to do", args.config)
        sys.exit(1)

    log.info("Loaded %d rule(s) from %s", len(rules), args.config)
    for threshold, scale in rules:
        log.debug("  SoC >= %.1f%% → scale %.3f", threshold, scale)

    if args.restore:
        log.info("--restore requested – will set StorCtl_Mod=0")

    # --- connect ------------------------------------------------------------
    log.info("Connecting to %s:%d (slave %d)", args.host, args.port, args.slave_id)
    device = sunspec_client.SunSpecModbusClientDeviceTCP(
        slave_id=args.slave_id,
        ipaddr=args.host,
        ipport=args.port,
    )

    try:
        device.scan()
    except Exception as exc:
        log.error("Device scan failed: %s", exc)
        sys.exit(1)

    # --- model 1 (Common) ---------------------------------------------------
    if 1 not in device.models:
        log.error("SunSpec model 1 (Common) not found on device – aborting")
        sys.exit(1)

    m1 = device.models[1][0]
    log.info("Device  manufacturer=%r  model=%r  serial=%r  firmware=%r",
             m1.Mn.value, m1.Md.value, m1.SN.value, m1.Vr.value)

    # --- model 124 (Basic Storage Controls) --------------------------------
    if 124 not in device.models:
        log.error("SunSpec model 124 (Basic Storage Controls) not found on device – aborting")
        sys.exit(1)

    storage = device.models[124][0]
    try:
        storage.read()
    except Exception as exc:
        log.error("Failed to read storage control model: %s", exc)
        sys.exit(1)

    # https://manuals.fronius.com/html/4204102649/#0_m_0000016556
    # WChaMax
    #    If there is a storage connected, this register contains the base value for OutWRte and InWRt.
    #    WChaMax := max(MaxChaRte, MaxDisChaRte)
    #
    #    If there is no storage connected, zero (0) is returned.

    soc: float = storage.ChaState.value * (10**storage.ChaState_SF.value)
    maxChargingValue: float = storage.WChaMax.value * (10**storage.WChaMax_SF.value) 
    log.info("Battery  SoC=%.1f%%  WChaMax=%.0f W",
             soc, maxChargingValue)

    if maxChargingValue == 0:
        log.info("WChaMax=%.0f W - is there a storage connected? Aborting.")
        return

    # --- deactivate shortcut ------------------------------------------------
    if args.deactivate:
        log.info("--deactivate requested – setting StorCtl_Mod=0")
        if not args.dry_run:
            try:
                storage.StorCtl_Mod.value = 0
                storage.write()
            except Exception as exc:
                log.error("Failed to write StorCtl_Mod: %s", exc)
                sys.exit(1)
            log.info("StorCtl_Mod written: 0")
        else:
            log.info("Dry-run active – skipping write")
        if not args.restore:
            return

    # --- restore shortcut ---------------------------------------------------
    if args.restore:
        log.info("Restoring max charge rate")
        if not args.dry_run:
            try:
                hundredPercentScaled: float = 100 * (10**abs(storage.InOutWRte_SF.value))
                outMax: float = 100 * (10**storage.InOutWRte_SF.value)
                storage.OutWRte.value = int(hundredPercentScaled)
                storage.InWRte.value = int(hundredPercentScaled)
                storage.StorCtl_Mod.value = 0
                storage.write()
            except Exception as exc:
                log.error("Failed to write charge rate: %s", exc)
                sys.exit(1)
            log.info("InWRte/OutWRte written (%.0f %% scaled), StorCtl_Mod=0", hundredPercentScaled)
        else:
            log.info("Dry-run active – skipping write")
        return

    # --- evaluate rules -----------------------------------------------------
    # Rules are already sorted highest-threshold first.
    matched_threshold: float | None = None
    matched_scale: float | None = None

    for threshold, scale in rules:
        if soc >= threshold:
            matched_threshold = threshold
            matched_scale = scale
            break

    if matched_scale is None:
        new_rate = 100 * (10**abs(storage.InOutWRte_SF.value))
        new_mod = 0   # no limit active
        log.info("SoC %.1f%% is below all thresholds – restoring full charge rate: %.0f W (%.0f %% scaled) mod: %.0f",
                 soc, maxChargingValue, new_rate, new_mod)
    else:
        lowerChargingLimitPercent: float = (args.min_charge_rate / maxChargingValue) * 100
        new_rate = max(lowerChargingLimitPercent, matched_scale * 100) * (10**abs(storage.InOutWRte_SF.value))
        new_mod = 1   # enable charging limit
        log.info("SoC %.1f%% >= threshold %.1f%% (scale %.3f) → "
                 "charge rate limit: %.0f %% scaled (lower limit: %.3f %%, cfg %.3f %%; max: %.0f W, min: %.0f W) mod: %.0f",
                 soc, matched_threshold, matched_scale, new_rate,
                 lowerChargingLimitPercent, matched_scale * 100,
                 maxChargingValue, args.min_charge_rate, new_mod)

    # --- apply --------------------------------------------------------------
    if args.dry_run:
        log.info("Dry-run active – skipping write")
        return

    try:
        storage.InWRte.value = int(new_rate)
        storage.StorCtl_Mod.value = int(new_mod)
        storage.write()
    except Exception as exc:
        log.error("Failed to write new charge rate: %s", exc)
        sys.exit(1)

    log.info("InWRte written: %.0f %% scaled  StorCtl_Mod=%.0f", new_rate, new_mod)


if __name__ == "__main__":
    main()
