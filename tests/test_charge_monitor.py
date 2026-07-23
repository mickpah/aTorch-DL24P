"""Tests for the CC-CV charge termination state machine."""

from load_test_bench.automation.charge_monitor import ChargeMonitor, ChargeState
from load_test_bench.protocol.dp832a_protocol import ChargerStatus


def make_status(current_a=1.0, mode="CC", output_on=True):
    return ChargerStatus(
        voltage_v=4.2,
        current_a=current_a,
        power_w=4.2 * current_a,
        output_on=output_on,
        mode=mode,
        channel=1,
    )


class TestChargeMonitor:
    def test_starts_idle(self):
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=3600)
        assert monitor.state == ChargeState.IDLE
        assert monitor.elapsed_s(100.0) == 0.0

    def test_charging_after_start(self):
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=3600)
        monitor.start(now_s=100.0)
        assert monitor.state == ChargeState.CHARGING
        assert monitor.update(make_status(current_a=1.0), now_s=101.0) == ChargeState.CHARGING
        assert monitor.elapsed_s(101.0) == 1.0

    def test_completes_after_consecutive_taper_samples(self):
        """CV mode with current at/below cutoff for taper_samples ticks ends charge."""
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=3600, taper_samples=5)
        monitor.start(now_s=0.0)
        for tick in range(4):
            state = monitor.update(make_status(current_a=0.04, mode="CV"), now_s=tick + 1)
            assert state == ChargeState.CHARGING
        assert monitor.update(make_status(current_a=0.04, mode="CV"), now_s=5.0) == ChargeState.COMPLETE

    def test_taper_count_resets_on_current_blip(self):
        """A single high-current sample restarts the taper count."""
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=3600, taper_samples=3)
        monitor.start(now_s=0.0)
        monitor.update(make_status(current_a=0.04, mode="CV"), now_s=1.0)
        monitor.update(make_status(current_a=0.04, mode="CV"), now_s=2.0)
        monitor.update(make_status(current_a=0.50, mode="CV"), now_s=3.0)  # blip
        monitor.update(make_status(current_a=0.04, mode="CV"), now_s=4.0)
        assert monitor.update(make_status(current_a=0.04, mode="CV"), now_s=5.0) == ChargeState.CHARGING

    def test_low_current_in_cc_mode_does_not_complete(self):
        """Taper only counts in CV mode - CC means the battery is still pulling."""
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=3600, taper_samples=2)
        monitor.start(now_s=0.0)
        for tick in range(5):
            state = monitor.update(make_status(current_a=0.01, mode="CC"), now_s=tick + 1)
        assert state == ChargeState.CHARGING

    def test_safety_timeout(self):
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=100.0)
        monitor.start(now_s=0.0)
        assert monitor.update(make_status(), now_s=99.0) == ChargeState.CHARGING
        assert monitor.update(make_status(), now_s=100.0) == ChargeState.TIMED_OUT

    def test_output_off_is_fault(self):
        """If the output drops (OVP trip, front-panel off), flag a fault."""
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=3600)
        monitor.start(now_s=0.0)
        assert monitor.update(make_status(output_on=False), now_s=1.0) == ChargeState.FAULT

    def test_terminal_states_are_sticky(self):
        monitor = ChargeMonitor(termination_current_a=0.05, timeout_s=100.0)
        monitor.start(now_s=0.0)
        monitor.update(make_status(), now_s=100.0)
        assert monitor.update(make_status(current_a=0.01, mode="CV"), now_s=101.0) == ChargeState.TIMED_OUT
