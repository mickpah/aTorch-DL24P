"""Tests for device role protocols and the registry."""

import pytest

from load_test_bench.jobs.devices import (
    DeviceRegistry,
    LoadDevice,
    MeterDevice,
    MeterStatus,
    PsuDevice,
)
from load_test_bench.protocol.device import USBHIDDevice
from load_test_bench.protocol.rigol_dp832a import RigolDP832A
from tests.fakes import FakeLoad, FakeMeter, FakePsu


class TestProtocolConformance:
    def test_usbhid_device_is_a_load_device(self):
        """The real DL24 driver satisfies the LoadDevice protocol."""
        assert isinstance(USBHIDDevice(), LoadDevice)

    def test_rigol_is_a_psu_device(self):
        assert isinstance(RigolDP832A(), PsuDevice)

    def test_fakes_conform(self):
        assert isinstance(FakeLoad(), LoadDevice)
        assert isinstance(FakePsu(), PsuDevice)
        assert isinstance(FakeMeter(), MeterDevice)


class TestFakeBehavior:
    def test_fake_load_records_calls_and_state(self):
        load = FakeLoad()
        assert load.turn_on() is True
        assert load.on is True
        assert load.set_current(1.5) is True
        assert ("set_current", 1.5) in load.calls
        load.turn_off()
        assert load.on is False

    def test_fake_command_failure_injection(self):
        load = FakeLoad()
        load.fail_commands = 2
        assert load.turn_on() is False
        assert load.set_current(1.0) is False
        assert load.turn_on() is True

    def test_fake_psu_output_state(self):
        psu = FakePsu()
        psu.output_on()
        assert psu.output_on_state is True
        psu.output_off()
        assert psu.output_on_state is False


class TestDeviceRegistry:
    def test_register_get_unregister(self):
        registry = DeviceRegistry()
        load = FakeLoad()
        registry.register("load", load)
        assert registry.load is load
        assert registry.get("load") is load
        registry.unregister("load")
        assert registry.load is None

    def test_unknown_role_rejected(self):
        registry = DeviceRegistry()
        with pytest.raises(ValueError):
            registry.register("oscilloscope", FakeLoad())

    def test_meter_status_fields(self):
        status = MeterStatus(voltage_v=4.19)
        assert status.voltage_v == 4.19
        assert status.current_a is None
