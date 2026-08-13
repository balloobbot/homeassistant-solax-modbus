"""Tests for totals surviving a Home Assistant restart."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from homeassistant.components.sensor import SensorExtraStoredData, SensorStateClass
from homeassistant.const import UnitOfEnergy

from custom_components.solax_modbus.const import REG_HOLDING, BaseModbusSensorEntityDescription
from custom_components.solax_modbus.sensor import (
    SolaXModbusRestoreSensor,
    SolaXModbusSensor,
    entityToListSingle,
)


def make_hub() -> Any:
    return SimpleNamespace(
        data={},
        sensorDescriptions={},
        sensorEntities={},
        computedSensors={},
        entity_dependencies={},
        sleepnone=[],
        sleepzero=[],
        _hass=None,
        name="test",
        scan_group=Mock(return_value="fast"),
        device_group_key=Mock(return_value="inverter"),
        async_add_solax_modbus_sensor=AsyncMock(),
    )


def total_description(key: str = "grid_export_energy_total") -> BaseModbusSensorEntityDescription:
    return BaseModbusSensorEntityDescription(
        name="Grid Export Energy Total",
        key=key,
        register=0x10,
        register_type=REG_HOLDING,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


def make_sensor(hub: Any, description: BaseModbusSensorEntityDescription) -> SolaXModbusRestoreSensor:
    sensor = SolaXModbusRestoreSensor("SolaX", hub, cast(Any, None), description)
    cast(Any, sensor).async_write_ha_state = Mock()
    return sensor


def build(hub: Any, description: BaseModbusSensorEntityDescription) -> Any:
    entities: list[Any] = []
    entityToListSingle(hub, "SolaX", entities, {}, {}, cast(Any, None), description, None, None)
    return entities[0]


def test_a_total_becomes_a_restore_sensor() -> None:
    assert isinstance(build(make_hub(), total_description()), SolaXModbusRestoreSensor)


def test_a_measurement_stays_a_plain_sensor() -> None:
    description = BaseModbusSensorEntityDescription(
        name="Grid Power",
        key="grid_power",
        register=0x20,
        register_type=REG_HOLDING,
        state_class=SensorStateClass.MEASUREMENT,
    )
    sensor = build(make_hub(), description)

    assert isinstance(sensor, SolaXModbusSensor)
    assert not isinstance(sensor, SolaXModbusRestoreSensor)


async def test_restore_seeds_the_hub_snapshot() -> None:
    """Without this the total reads "unknown" until its block is polled again."""
    hub = make_hub()
    sensor = make_sensor(hub, total_description())
    cast(Any, sensor).async_get_last_sensor_data = AsyncMock(return_value=SensorExtraStoredData(2.44, UnitOfEnergy.KILO_WATT_HOUR))

    await sensor.async_added_to_hass()

    assert hub.data["grid_export_energy_total"] == 2.44
    assert sensor.native_value == 2.44


async def test_restore_does_not_clobber_a_polled_value() -> None:
    hub = make_hub()
    hub.data["grid_export_energy_total"] = 9.99
    sensor = make_sensor(hub, total_description())
    cast(Any, sensor).async_get_last_sensor_data = AsyncMock(return_value=SensorExtraStoredData(2.44, UnitOfEnergy.KILO_WATT_HOUR))

    await sensor.async_added_to_hass()

    assert hub.data["grid_export_energy_total"] == 9.99


async def test_nothing_to_restore_leaves_the_snapshot_alone() -> None:
    hub = make_hub()
    sensor = make_sensor(hub, total_description())
    cast(Any, sensor).async_get_last_sensor_data = AsyncMock(return_value=None)

    await sensor.async_added_to_hass()

    assert "grid_export_energy_total" not in hub.data
