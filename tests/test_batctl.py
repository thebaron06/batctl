"""
Unit tests for batctl.py — pure-logic functions only; no hardware, no network.
"""

import datetime
import json
import os
import tempfile
from zoneinfo import ZoneInfo

import pytest

# Make sure we can import from the parent directory
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from batctl import (
    Config,
    PHASE_DAY,
    PHASE_END_OF_DAY,
    PHASE_NIGHT,
    _is_weather_cache_valid,
    compute_charge_setpoint,
    compute_discharge_setpoint,
    determine_phase,
    estimate_tomorrow_kwh,
    load_config,
    update_config_file,
    validate_features,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dt(hour: int, minute: int = 0) -> datetime.datetime:
    """Create a tz-aware datetime for today at the given time (UTC)."""
    tz = ZoneInfo("UTC")
    return datetime.datetime.now(tz=tz).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


def _sunrise() -> datetime.datetime:
    return _dt(6, 0)   # 06:00 UTC


def _sunset() -> datetime.datetime:
    return _dt(20, 0)  # 20:00 UTC


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_defaults_when_file_absent(self):
        cfg = load_config("/nonexistent/path/batctl.conf")
        assert cfg.port == 502
        assert cfg.slave_id == 1
        assert cfg.feat_charge_limit is False
        assert cfg.feat_night_grid_export is False
        assert cfg.reserve_soc == 12.0
        assert cfg.performance_ratio == 0.75

    def test_reads_ini_values(self, tmp_path):
        conf = tmp_path / "batctl.conf"
        conf.write_text(
            "[connection]\nhost = inverter.local\nport = 1502\n"
            "[features]\ncharge_limit = true\n"
            "[charging]\nupper_soc_limit = 75\n"
        )
        cfg = load_config(str(conf))
        assert cfg.host == "inverter.local"
        assert cfg.port == 1502
        assert cfg.feat_charge_limit is True
        assert cfg.upper_soc_limit == 75.0

    def test_cli_overrides_file(self, tmp_path):
        conf = tmp_path / "batctl.conf"
        conf.write_text("[connection]\nhost = original\n")
        cfg = load_config(str(conf), {"connection.host": "override"})
        assert cfg.host == "override"

    def test_charge_taper_parsed(self, tmp_path):
        conf = tmp_path / "batctl.conf"
        conf.write_text("[charge_taper]\n95 = 0.05\n80 = 0.30\n")
        cfg = load_config(str(conf))
        assert len(cfg.charge_taper) == 2
        # Sorted descending by threshold
        assert cfg.charge_taper[0] == (95.0, 0.05)
        assert cfg.charge_taper[1] == (80.0, 0.30)

    def test_invalid_taper_entries_skipped(self, tmp_path):
        conf = tmp_path / "batctl.conf"
        conf.write_text("[charge_taper]\n95 = 0.05\nnot_a_number = 0.5\n80 = 1.5\n")
        cfg = load_config(str(conf))
        # Only 95=0.05 is valid; 'not_a_number' and scale 1.5 are rejected
        assert len(cfg.charge_taper) == 1
        assert cfg.charge_taper[0] == (95.0, 0.05)

    def test_location_parsed(self, tmp_path):
        conf = tmp_path / "batctl.conf"
        conf.write_text("[location]\nlatitude = 48.21\nlongitude = 16.37\ntimezone = Europe/Vienna\n")
        cfg = load_config(str(conf))
        assert cfg.latitude == pytest.approx(48.21)
        assert cfg.longitude == pytest.approx(16.37)
        assert cfg.timezone == "Europe/Vienna"

    def test_empty_location_gives_none(self, tmp_path):
        conf = tmp_path / "batctl.conf"
        conf.write_text("")
        cfg = load_config(str(conf))
        assert cfg.latitude is None
        assert cfg.longitude is None


# ---------------------------------------------------------------------------
# update_config_file
# ---------------------------------------------------------------------------

class TestUpdateConfigFile:
    def test_updates_existing_key(self, tmp_path):
        f = tmp_path / "conf.ini"
        f.write_text("[battery]\ncapacity_kwh = 0\nwchamax_w = 0\n")
        update_config_file(str(f), {"battery": {"capacity_kwh": "7.700"}})
        text = f.read_text()
        assert "capacity_kwh = 7.700" in text
        assert "wchamax_w = 0" in text  # unchanged

    def test_adds_missing_section(self, tmp_path):
        f = tmp_path / "conf.ini"
        f.write_text("[connection]\nhost = x\n")
        update_config_file(str(f), {"detected": {"manufacturer": "Fronius"}})
        text = f.read_text()
        assert "[detected]" in text
        assert "manufacturer = Fronius" in text

    def test_preserves_comments(self, tmp_path):
        f = tmp_path / "conf.ini"
        f.write_text("# top comment\n[battery]\n# inline comment\ncapacity_kwh = 0\n")
        update_config_file(str(f), {"battery": {"capacity_kwh": "7.7"}})
        text = f.read_text()
        assert "# top comment" in text
        assert "# inline comment" in text
        assert "capacity_kwh = 7.7" in text

    def test_creates_file_if_absent(self, tmp_path):
        f = tmp_path / "new.ini"
        update_config_file(str(f), {"battery": {"capacity_kwh": "5.1"}})
        assert "capacity_kwh = 5.1" in f.read_text()


# ---------------------------------------------------------------------------
# validate_features
# ---------------------------------------------------------------------------

class TestValidateFeatures:
    def _base(self, **overrides) -> Config:
        """Return a Config with valid common settings."""
        cfg = Config(
            latitude=48.21,
            longitude=16.37,
            timezone="Europe/Vienna",
            capacity_kwh=7.7,
            pv_peak_kwp=8.0,
            performance_ratio=0.75,
            skip_export_below_kwh=10.0,
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg

    def test_all_features_off_ok(self):
        validate_features(Config())  # should not raise

    def test_charge_limit_alone_ok(self):
        cfg = self._base(feat_charge_limit=True)
        validate_features(cfg)  # no location needed for charge_limit alone

    def test_end_of_day_requires_charge_limit(self):
        cfg = self._base(feat_end_of_day_full_charge=True, feat_charge_limit=False)
        with pytest.raises(ValueError, match="charge_limit"):
            validate_features(cfg)

    def test_end_of_day_requires_location(self):
        cfg = self._base(
            feat_charge_limit=True,
            feat_end_of_day_full_charge=True,
            latitude=None,
        )
        with pytest.raises(ValueError, match="location"):
            validate_features(cfg)

    def test_night_export_requires_location(self):
        cfg = self._base(feat_night_grid_export=True, latitude=None)
        with pytest.raises(ValueError, match="location"):
            validate_features(cfg)

    def test_night_export_requires_capacity(self):
        cfg = self._base(feat_night_grid_export=True, capacity_kwh=0.0)
        with pytest.raises(ValueError, match="capacity_kwh"):
            validate_features(cfg)

    def test_weather_requires_night_export(self):
        cfg = self._base(feat_weather_aware=True, feat_night_grid_export=False)
        with pytest.raises(ValueError, match="night_grid_export"):
            validate_features(cfg)

    def test_weather_requires_kwp(self):
        cfg = self._base(feat_night_grid_export=True, feat_weather_aware=True, pv_peak_kwp=0.0)
        with pytest.raises(ValueError, match="pv_peak_kwp"):
            validate_features(cfg)

    def test_all_features_enabled_ok(self):
        cfg = self._base(
            feat_charge_limit=True,
            feat_end_of_day_full_charge=True,
            feat_night_grid_export=True,
            feat_weather_aware=True,
        )
        validate_features(cfg)  # should not raise


# ---------------------------------------------------------------------------
# determine_phase
# ---------------------------------------------------------------------------

class TestDeterminePhase:
    def test_before_sunrise_is_night(self):
        assert determine_phase(_dt(5, 59), _sunrise(), _sunset(), 120) == PHASE_NIGHT

    def test_at_sunrise_is_day(self):
        assert determine_phase(_dt(6, 0), _sunrise(), _sunset(), 120) == PHASE_DAY

    def test_mid_day_is_day(self):
        assert determine_phase(_dt(13, 0), _sunrise(), _sunset(), 120) == PHASE_DAY

    def test_just_before_eod_window_is_day(self):
        # lead=120 min → eod starts at 18:00 (sunset 20:00 - 2h)
        assert determine_phase(_dt(17, 59), _sunrise(), _sunset(), 120) == PHASE_DAY

    def test_at_eod_window_start_is_eod(self):
        assert determine_phase(_dt(18, 0), _sunrise(), _sunset(), 120) == PHASE_END_OF_DAY

    def test_just_before_sunset_is_eod(self):
        assert determine_phase(_dt(19, 59), _sunrise(), _sunset(), 120) == PHASE_END_OF_DAY

    def test_at_sunset_is_night(self):
        assert determine_phase(_dt(20, 0), _sunrise(), _sunset(), 120) == PHASE_NIGHT

    def test_deep_night_is_night(self):
        assert determine_phase(_dt(23, 0), _sunrise(), _sunset(), 120) == PHASE_NIGHT

    def test_zero_lead_time(self):
        # With lead=0, EOD window is zero-width → no EOD phase
        assert determine_phase(_dt(19, 59), _sunrise(), _sunset(), 0) == PHASE_DAY
        assert determine_phase(_dt(20, 0), _sunrise(), _sunset(), 0) == PHASE_NIGHT


# ---------------------------------------------------------------------------
# compute_charge_setpoint
# ---------------------------------------------------------------------------

class TestComputeChargeSetpoint:
    # SF=-2 → max_raw = int(100 * 10^2) = 10000
    SF = -2
    WCHAMAX = 5000.0

    def test_no_feature_no_taper_gives_full_rate(self):
        inw, mod = compute_charge_setpoint(
            soc=50.0, taper=[], upper_soc_limit=80.0,
            min_charge_rate_w=300.0, wchamax_w=self.WCHAMAX,
            sf=self.SF, feat_charge_limit=False,
        )
        assert inw == 10000
        assert mod == 0

    def test_taper_match_applies_scale(self):
        taper = [(95.0, 0.05), (80.0, 0.30)]
        inw, mod = compute_charge_setpoint(
            soc=85.0, taper=taper, upper_soc_limit=99.0,
            min_charge_rate_w=0.0, wchamax_w=self.WCHAMAX,
            sf=self.SF, feat_charge_limit=False,
        )
        # scale 0.30 × 100% × 10^2 = 3000
        assert inw == 3000
        assert mod == 1

    def test_taper_min_charge_floor(self):
        taper = [(95.0, 0.01)]  # 1% → 50 W < min 300 W → 6%
        inw, mod = compute_charge_setpoint(
            soc=96.0, taper=taper, upper_soc_limit=99.0,
            min_charge_rate_w=300.0, wchamax_w=self.WCHAMAX,
            sf=self.SF, feat_charge_limit=False,
        )
        # min_rate_pct = 300/5000*100 = 6.0% → raw = 600
        assert inw == 600
        assert mod == 1

    def test_upper_limit_stops_charging(self):
        taper = [(95.0, 0.05)]
        inw, mod = compute_charge_setpoint(
            soc=82.0, taper=taper, upper_soc_limit=80.0,
            min_charge_rate_w=300.0, wchamax_w=self.WCHAMAX,
            sf=self.SF, feat_charge_limit=True,
        )
        assert inw == 0
        assert mod == 1

    def test_upper_limit_inactive_below_limit(self):
        taper = [(95.0, 0.05), (80.0, 0.30)]
        inw, mod = compute_charge_setpoint(
            soc=70.0, taper=taper, upper_soc_limit=80.0,
            min_charge_rate_w=0.0, wchamax_w=self.WCHAMAX,
            sf=self.SF, feat_charge_limit=True,
        )
        # Below all taper thresholds → full rate
        assert inw == 10000
        assert mod == 0


# ---------------------------------------------------------------------------
# compute_discharge_setpoint
# ---------------------------------------------------------------------------

class TestComputeDischargeSetpoint:
    SF = -2
    WCHAMAX = 5000.0   # 5 kW
    CAPACITY = 7.7     # kWh

    def test_at_reserve_returns_none(self):
        result = compute_discharge_setpoint(
            soc=12.0, reserve_soc=12.0, capacity_kwh=self.CAPACITY,
            hours_left=8.0, wchamax_w=self.WCHAMAX,
            min_discharge_w=300.0, sf=self.SF,
        )
        assert result is None

    def test_below_reserve_returns_none(self):
        result = compute_discharge_setpoint(
            soc=10.0, reserve_soc=12.0, capacity_kwh=self.CAPACITY,
            hours_left=8.0, wchamax_w=self.WCHAMAX,
            min_discharge_w=300.0, sf=self.SF,
        )
        assert result is None

    def test_zero_hours_returns_none(self):
        result = compute_discharge_setpoint(
            soc=80.0, reserve_soc=12.0, capacity_kwh=self.CAPACITY,
            hours_left=0.0, wchamax_w=self.WCHAMAX,
            min_discharge_w=300.0, sf=self.SF,
        )
        assert result is None

    def test_normal_case_computes_rate(self):
        # energy = (80-12)/100 * 7.7 = 5.236 kWh
        # required_w = 5236 / 8 = 654.5 W
        # rate_pct = 654.5/5000*100 = 13.09%
        # raw = round(13.09 * 100) = 1309
        result = compute_discharge_setpoint(
            soc=80.0, reserve_soc=12.0, capacity_kwh=self.CAPACITY,
            hours_left=8.0, wchamax_w=self.WCHAMAX,
            min_discharge_w=0.0, sf=self.SF,
        )
        assert result is not None
        energy = (80.0 - 12.0) / 100.0 * 7.7
        required_w = energy * 1000.0 / 8.0
        expected_raw = round(required_w / 5000.0 * 100.0 * 100)
        assert result == expected_raw

    def test_min_rate_floor_applied(self):
        # Very small SOC margin: required_w would be tiny → floor kicks in
        result = compute_discharge_setpoint(
            soc=13.0, reserve_soc=12.0, capacity_kwh=self.CAPACITY,
            hours_left=8.0, wchamax_w=self.WCHAMAX,
            min_discharge_w=300.0, sf=self.SF,
        )
        # min_pct = 300/5000*100 = 6%  → raw = 600
        assert result is not None
        assert result == 600

    def test_rate_capped_at_100_percent(self):
        # Very little time left → required_w exceeds WChaMax
        result = compute_discharge_setpoint(
            soc=100.0, reserve_soc=12.0, capacity_kwh=self.CAPACITY,
            hours_left=0.01, wchamax_w=self.WCHAMAX,
            min_discharge_w=0.0, sf=self.SF,
        )
        assert result == 10000  # 100% × 100 (SF=-2)

    def test_sf_zero(self):
        result = compute_discharge_setpoint(
            soc=50.0, reserve_soc=12.0, capacity_kwh=self.CAPACITY,
            hours_left=8.0, wchamax_w=self.WCHAMAX,
            min_discharge_w=0.0, sf=0,
        )
        # SF=0 → raw = round(rate_pct * 1)
        energy = (50.0 - 12.0) / 100.0 * 7.7
        required_w = energy * 1000.0 / 8.0
        expected_raw = round(required_w / 5000.0 * 100.0)
        assert result == expected_raw


# ---------------------------------------------------------------------------
# estimate_tomorrow_kwh
# ---------------------------------------------------------------------------

class TestEstimateTomorrowKwh:
    def test_known_values(self):
        # 18 MJ/m² / 3.6 = 5 kWh/m², × 8 kWp × 0.75 = 30 kWh
        result = estimate_tomorrow_kwh(18.0, 8.0, 0.75)
        assert result == pytest.approx(30.0, rel=1e-6)

    def test_zero_radiation(self):
        assert estimate_tomorrow_kwh(0.0, 8.0, 0.75) == pytest.approx(0.0)

    def test_pr_scaling(self):
        r1 = estimate_tomorrow_kwh(10.0, 8.0, 0.5)
        r2 = estimate_tomorrow_kwh(10.0, 8.0, 1.0)
        assert r2 == pytest.approx(r1 * 2.0, rel=1e-6)


# ---------------------------------------------------------------------------
# _is_weather_cache_valid
# ---------------------------------------------------------------------------

class TestWeatherCacheValid:
    def _cfg(self) -> Config:
        return Config(latitude=48.21, longitude=16.37, timezone="Europe/Vienna", refresh_hours=6.0)

    def _cache(self, today: datetime.date, hours_old: float = 1.0) -> dict:
        tomorrow = (today + datetime.timedelta(days=1)).isoformat()
        fetched_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_old)
        return {
            "latitude": 48.21,
            "longitude": 16.37,
            "fetched_at": fetched_at.isoformat(),
            "target_date": tomorrow,
            "expected_kwh": 25.0,
        }

    def test_fresh_cache_is_valid(self):
        today = datetime.date.today()
        assert _is_weather_cache_valid(self._cache(today, 1.0), self._cfg(), today)

    def test_stale_cache_invalid(self):
        today = datetime.date.today()
        assert not _is_weather_cache_valid(self._cache(today, 7.0), self._cfg(), today)

    def test_wrong_location_invalid(self):
        today = datetime.date.today()
        cache = self._cache(today)
        cache["latitude"] = 50.0  # different location
        assert not _is_weather_cache_valid(cache, self._cfg(), today)

    def test_wrong_date_invalid(self):
        today = datetime.date.today()
        cache = self._cache(today)
        cache["target_date"] = "2000-01-01"  # stale date
        assert not _is_weather_cache_valid(cache, self._cfg(), today)

    def test_corrupt_cache_invalid(self):
        today = datetime.date.today()
        assert not _is_weather_cache_valid({}, self._cfg(), today)
        assert not _is_weather_cache_valid({"latitude": "bad"}, self._cfg(), today)
