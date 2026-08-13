"""Tests for the diagnostics download."""

import time as _mtime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from modbus_connection import ModbusTimeoutError

from custom_components.solax_modbus import COMM_BLOCK_FAILURE_WINDOW, SolaXModbusHub
from custom_components.solax_modbus.diagnostics import async_get_config_entry_diagnostics


def make_hub(*, holding: list[Any] | None = None, input_blocks: list[Any] | None = None) -> Any:
    """Build the minimal hub state required by the diagnostics payload."""
    hub = cast(Any, object.__new__(SolaXModbusHub))
    hub._name = "test"
    hub._modbus_addr = 1
    hub.data = {"communication_health": "Healthy", "communication_quarantined_registers": 0}
    hub.bad_regs = {"holding": set(), "input": set()}
    hub._comm_block_failures = {}
    hub.groups = {
        5: SimpleNamespace(
            device_groups={
                "inverter": SimpleNamespace(
                    holdingBlocks=holding if holding is not None else [],
                    inputBlocks=input_blocks if input_blocks is not None else [],
                )
            }
        )
    }
    return hub


def block(start: int, end: int) -> Any:
    return SimpleNamespace(start=start, end=end)


async def test_read_raw_maps_words_to_absolute_addresses() -> None:
    hub = make_hub(holding=[block(0x10, 0x13)], input_blocks=[block(0x400, 0x402)])
    hub._async_read_registers = AsyncMock(side_effect=[[11, 22, 33], [44, 55]])

    raw = await hub.async_read_raw()

    assert raw == {
        "holding": {0x10: 11, 0x11: 22, 0x12: 33},
        "input": {0x400: 44, 0x401: 55},
    }


async def test_read_raw_skips_a_failing_block() -> None:
    """A partial dump is worth more to a bug report than a failed download."""
    hub = make_hub(holding=[block(0x10, 0x12), block(0x20, 0x21)])
    hub._async_read_registers = AsyncMock(side_effect=[ModbusTimeoutError("silence"), [99]])

    raw = await hub.async_read_raw()

    assert raw == {"holding": {0x20: 99}, "input": {}}


def test_failed_blocks_drop_out_of_the_window() -> None:
    hub = make_hub()
    now = _mtime.time()
    hub._comm_block_failures = {
        "holding:0x10-0x12": [now],
        "input:0x400-0x402": [now - COMM_BLOCK_FAILURE_WINDOW - 1],
    }

    assert hub.communication_failed_blocks() == ["holding:0x10-0x12"]


async def test_diagnostics_report_the_poll_outcome_and_registers(hass: Any) -> None:
    hub = make_hub(holding=[block(0x10, 0x11)])
    hub._async_read_registers = AsyncMock(return_value=[7])
    hub._comm_block_failures = {"input:0x400-0x402": [_mtime.time()]}
    hub._comm_last_error = "input:0x400-0x402: exception timeout"
    hub._comm_last_error_time = "2026-08-14 00:00:00"
    hub._comm_poll_durations = [120]
    hub._comm_overrun_count = 0
    hub._comm_recovery_active = False
    hub._comm_last_block_failure_time = None
    hub._comm_last_quarantined_register = None
    hub._comm_last_recovered_register = None
    hub._time_out = 6
    hub.plugin = SimpleNamespace(plugin_name="SolaX", plugin_manufacturer="SolaX Power")
    hub.invertertype = 3

    hass.data["solax_modbus"] = {"MyInverter": {"hub": hub}}
    entry = cast(Any, SimpleNamespace(options={"name": "MyInverter"}, data={}))

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["registers"] == {"holding": {0x10: 7}, "input": {}}
    assert result["poll"]["health"] == "Healthy"
    assert result["poll"]["failed_blocks"] == ["input:0x400-0x402"]
    assert result["poll"]["last_error"] == "input:0x400-0x402: exception timeout"
    assert result["inverter"]["plugin"] == "SolaX"


async def test_diagnostics_never_carry_the_host_or_serial(hass: Any) -> None:
    """The payload is built by hand precisely so identifying data cannot leak."""
    hub = make_hub()
    hub._comm_last_error = None
    hub._comm_last_error_time = None
    hub._comm_poll_durations = []
    hub._comm_overrun_count = 0
    hub._comm_recovery_active = False
    hub._comm_last_block_failure_time = None
    hub._comm_last_quarantined_register = None
    hub._comm_last_recovered_register = None
    hub._time_out = 6
    hub.plugin = SimpleNamespace(plugin_name="SolaX", plugin_manufacturer="SolaX Power")
    hub.invertertype = 3

    hass.data["solax_modbus"] = {"MyInverter": {"hub": hub}}
    entry = cast(Any, SimpleNamespace(options={"name": "MyInverter", "host": "192.168.1.50"}, data={}))

    dumped = repr(await async_get_config_entry_diagnostics(hass, entry))

    assert "192.168.1.50" not in dumped
