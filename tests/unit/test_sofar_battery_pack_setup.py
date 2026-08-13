"""Battery pack enumeration must survive a failed read during platform setup."""

from types import SimpleNamespace
from typing import Any

import pytest
from modbus_connection import ModbusTimeoutError

from custom_components.solax_modbus.plugin_sofar import battery_config


def make_hub(read: Any) -> Any:
    return SimpleNamespace(name="sofar", _modbus_addr=1, async_read_holding_registers=read)


@pytest.mark.asyncio
async def test_unreadable_pack_serial_is_reported_as_missing() -> None:
    # sensor.py enumerates packs inside async_setup_entry, which Home Assistant
    # does not retry: an escaping read error costs every sensor on the inverter.
    async def read(unit: int, address: int, count: int) -> list[int]:
        raise ModbusTimeoutError("no answer")

    assert await battery_config()._determinate_batt_pack_serial(make_hub(read)) is None


@pytest.mark.asyncio
async def test_readable_pack_serial_is_decoded() -> None:
    async def read(unit: int, address: int, count: int) -> list[int]:
        return [0x5041, 0x434B, 0x3031, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000]

    serial = await battery_config()._determinate_batt_pack_serial(make_hub(read))

    assert serial is not None
    assert serial.startswith("PACK01")
