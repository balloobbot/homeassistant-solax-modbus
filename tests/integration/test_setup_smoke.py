"""Boot the integration in Home Assistant against a mocked Modbus device."""

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from modbus_connection.mock import MockModbusConnection
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solax_modbus.const import (
    CONF_INTERFACE,
    CONF_MODBUS_ADDR,
    CONF_PLUGIN,
    CONF_TCP_TYPE,
    CONF_TIME_OUT,
    DOMAIN,
)

HUB_NAME = "Smoke"
MODBUS_ADDR = 1
# The srne plugin reads its serial from 0x35 and maps a "GEN" prefix to a hybrid.
SERIALNR_REGISTER = 0x35
SERIALNR_COUNT = 20
SERIALNR = "GEN-SMOKE-TEST"
# battery_voltage, scale 0.1 -> 53.2 V
BATTERY_VOLTAGE_REGISTER = 0x101
BATTERY_VOLTAGE_RAW = 532


def _encode_string(text: str, count: int) -> list[int]:
    """Encode text as decode_string reads it: two ASCII chars per register."""
    padded = text.ljust(count * 2, "\x00")
    return [(ord(padded[i]) << 8) | ord(padded[i + 1]) for i in range(0, count * 2, 2)]


@pytest.fixture
def mock_device() -> MockModbusConnection:
    """An in-memory inverter; unset registers read as zero."""
    connection = MockModbusConnection()
    unit = connection.for_unit(MODBUS_ADDR)
    unit.holding[SERIALNR_REGISTER] = _encode_string(SERIALNR, SERIALNR_COUNT)
    unit.holding[BATTERY_VOLTAGE_REGISTER] = BATTERY_VOLTAGE_RAW
    return connection


async def test_config_entry_loads_and_polls_a_real_value(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    mock_device: MockModbusConnection,
) -> None:
    """The entry reaches LOADED, entities are created and a poll decodes a register."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_NAME: HUB_NAME,
            CONF_PLUGIN: "srne",
            CONF_INTERFACE: "tcp",
            CONF_HOST: "127.0.0.1",
            CONF_PORT: 502,
            CONF_TCP_TYPE: "tcp",
            CONF_MODBUS_ADDR: MODBUS_ADDR,
            CONF_SCAN_INTERVAL: 15,
            CONF_TIME_OUT: 5,
        },
    )
    entry.add_to_hass(hass)

    # Inject at the library boundary: everything above it (link sharing,
    # transport, unit selection, decoding) runs for real.
    with patch(
        "custom_components.solax_modbus.modbus_link.TmodbusConnection",
        return_value=mock_device,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    hub: Any = hass.data[DOMAIN][HUB_NAME]["hub"]
    assert hub.seriesnumber == SERIALNR

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{HUB_NAME}_battery_voltage")
    assert entity_id is not None, "battery_voltage sensor was never created"

    await hub._refresh_interval_group_once(hub.groups[15], bypass_slowdown=True)
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == pytest.approx(53.2)
