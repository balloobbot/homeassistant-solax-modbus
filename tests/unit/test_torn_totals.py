"""Tests for a total ignoring a read torn by a mid-update counter."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from homeassistant.components.sensor import SensorStateClass

from custom_components.solax_modbus import SolaXModbusHub
from custom_components.solax_modbus.const import REGISTER_U16


def make_hub() -> Any:
    """A hub with only the state treat_address touches."""
    hub = cast(Any, object.__new__(SolaXModbusHub))
    hub._name = "test"
    hub.plugin = SimpleNamespace(order32="big", isAwake=lambda data: True)
    hub.cyclecount = 100
    hub.tmpdata_expiry = {}
    hub.localsLoaded = True
    hub.inverterPowerKw = 100
    hub._validate_register_func = None
    return hub


def description(state_class: SensorStateClass) -> Any:
    return SimpleNamespace(
        key="energy_total",
        register=0x10,
        register_data_type=REGISTER_U16,
        read_scale=1,
        scale=0.1,
        rounding=1,
        wordcount=None,
        sleepmode=None,
        native_unit_of_measurement=None,
        min_value=None,
        max_value=None,
        read_scale_exceptions=None,
        state_class=state_class,
    )


@pytest.mark.parametrize(
    ("state_class", "expected"),
    [
        (SensorStateClass.TOTAL_INCREASING, 100.0),
        # A TOTAL may legitimately fall, so it follows the inverter down
        (SensorStateClass.TOTAL, 99.5),
    ],
)
def test_a_small_dip_is_ignored_only_for_a_total_increasing(state_class: SensorStateClass, expected: float) -> None:
    hub = make_hub()
    descr = description(state_class)
    data: dict[str, Any] = {}

    hub.treat_address(data, [1000], 0, descr)
    assert data["energy_total"] == 100.0

    hub.treat_address(data, [995], 0, descr)
    assert data["energy_total"] == expected


def test_a_real_reset_is_published() -> None:
    hub = make_hub()
    descr = description(SensorStateClass.TOTAL_INCREASING)
    data: dict[str, Any] = {}

    hub.treat_address(data, [1000], 0, descr)
    hub.treat_address(data, [900], 0, descr)

    # More than 1% down is the meter itself resetting, which Home Assistant has to see
    assert data["energy_total"] == 90.0


def test_a_total_increasing_still_rises() -> None:
    hub = make_hub()
    descr = description(SensorStateClass.TOTAL_INCREASING)
    data: dict[str, Any] = {}

    hub.treat_address(data, [1000], 0, descr)
    hub.treat_address(data, [1010], 0, descr)

    assert data["energy_total"] == 101.0


def test_a_restored_total_is_the_mark_for_the_first_poll() -> None:
    hub = make_hub()
    descr = description(SensorStateClass.TOTAL_INCREASING)
    # SolaXModbusRestoreSensor seeds hub.data before the first poll, so the mark survives a restart
    data: dict[str, Any] = {"energy_total": 100.0}

    hub.treat_address(data, [995], 0, descr)

    assert data["energy_total"] == 100.0
