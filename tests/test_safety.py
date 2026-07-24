"""Tests for the actuating safety layer (rules + latching supervisor)."""

from dataclasses import dataclass

from load_test_bench.jobs.safety import (
    SafetyConfig,
    SafetyRules,
    SafetySupervisor,
)


@dataclass
class LoadStatus:
    mosfet_temp_c: int = 40
    ext_temp_c: int = 25
    load_on: bool = True


@dataclass
class PsuStatus:
    current_a: float = 1.0
    output_on: bool = True


class TestSafetyRules:
    def test_mosfet_over_temp_trips(self):
        rules = SafetyRules(SafetyConfig(mosfet_temp_max_c=80.0))
        trips = rules.evaluate_load(LoadStatus(mosfet_temp_c=80))
        assert len(trips) == 1
        assert trips[0].rule == "mosfet_over_temp"

    def test_below_threshold_is_quiet(self):
        rules = SafetyRules(SafetyConfig(mosfet_temp_max_c=80.0))
        assert rules.evaluate_load(LoadStatus(mosfet_temp_c=79)) == []

    def test_ext_probe_rule_disabled_when_none(self):
        rules = SafetyRules(SafetyConfig(ext_temp_max_c=None))
        assert rules.evaluate_load(LoadStatus(ext_temp_c=200)) == []

    def test_ext_probe_zero_reading_means_absent(self):
        """A 0 reading means no probe attached - never a trip."""
        rules = SafetyRules(SafetyConfig(ext_temp_max_c=60.0))
        assert rules.evaluate_load(LoadStatus(ext_temp_c=0)) == []
        assert len(rules.evaluate_load(LoadStatus(ext_temp_c=60))) == 1

    def test_psu_ceiling_disabled_by_default(self):
        rules = SafetyRules(SafetyConfig())
        assert rules.evaluate_psu(PsuStatus(current_a=99.0)) == []

    def test_psu_ceiling_trips_when_configured(self):
        rules = SafetyRules(SafetyConfig(psu_current_max_a=2.0))
        trips = rules.evaluate_psu(PsuStatus(current_a=2.5))
        assert len(trips) == 1
        assert trips[0].rule == "psu_over_current"

    def test_clear_requires_hysteresis_margin(self):
        rules = SafetyRules(SafetyConfig(mosfet_temp_max_c=80.0, temp_hysteresis_c=5.0))
        assert rules.is_clear_load(LoadStatus(mosfet_temp_c=78)) is False  # < 80 but not < 75
        assert rules.is_clear_load(LoadStatus(mosfet_temp_c=74)) is True


class TestSafetySupervisor:
    def make(self, **config):
        reasons = []
        supervisor = SafetySupervisor(
            SafetyRules(SafetyConfig(**config)), on_trip=reasons.append
        )
        return supervisor, reasons

    def test_trip_latches_and_fires_once(self):
        supervisor, reasons = self.make(mosfet_temp_max_c=80.0)
        supervisor.observe_load(LoadStatus(mosfet_temp_c=85), now_s=1.0)
        supervisor.observe_load(LoadStatus(mosfet_temp_c=86), now_s=2.0)
        assert supervisor.tripped is True
        assert "mosfet" in supervisor.trip_reason
        assert len(reasons) == 1

    def test_stale_status_trips_when_output_believed_on(self):
        supervisor, reasons = self.make(stale_status_timeout_s=10.0)
        supervisor.observe_load(LoadStatus(load_on=True), now_s=0.0)
        supervisor.check_stale(now_s=9.0)
        assert supervisor.tripped is False
        supervisor.check_stale(now_s=10.0)
        assert supervisor.tripped is True
        assert "stale" in supervisor.trip_reason

    def test_no_stale_trip_when_output_off(self):
        supervisor, _ = self.make(stale_status_timeout_s=10.0)
        supervisor.observe_load(LoadStatus(load_on=False), now_s=0.0)
        supervisor.check_stale(now_s=100.0)
        assert supervisor.tripped is False

    def test_fresh_status_resets_staleness(self):
        supervisor, _ = self.make(stale_status_timeout_s=10.0)
        supervisor.observe_load(LoadStatus(load_on=True), now_s=0.0)
        supervisor.observe_load(LoadStatus(load_on=True), now_s=8.0)
        supervisor.check_stale(now_s=15.0)
        assert supervisor.tripped is False

    def test_reset_refused_while_condition_persists(self):
        supervisor, _ = self.make(mosfet_temp_max_c=80.0, temp_hysteresis_c=5.0)
        supervisor.observe_load(LoadStatus(mosfet_temp_c=85), now_s=1.0)
        assert supervisor.try_reset() is False
        supervisor.observe_load(LoadStatus(mosfet_temp_c=78), now_s=2.0)
        assert supervisor.try_reset() is False  # inside hysteresis band
        supervisor.observe_load(LoadStatus(mosfet_temp_c=70), now_s=3.0)
        assert supervisor.try_reset() is True
        assert supervisor.tripped is False

    def test_reset_when_not_tripped_is_true(self):
        supervisor, _ = self.make()
        assert supervisor.try_reset() is True

    def test_psu_observation_trips(self):
        supervisor, reasons = self.make(psu_current_max_a=2.0)
        supervisor.observe_psu(PsuStatus(current_a=3.0), now_s=1.0)
        assert supervisor.tripped is True
        assert len(reasons) == 1
