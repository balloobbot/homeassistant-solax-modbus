"""Tests for dropping a link the device has stopped answering on."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from modbus_connection import (
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusDesyncError,
    ModbusTimeoutError,
)

from custom_components.solax_modbus import SolaXModbusHub


def make_hub(*, connected: bool = True) -> Any:
    """Build a hub carrying only the transport state the link tests touch."""
    hub = cast(Any, object.__new__(SolaXModbusHub))
    hub._name = "test"
    hub._stopping = False
    hub._unanswered_requests = 0
    hub._inflight_tasks = set()
    hub._transport = SimpleNamespace(
        is_connected=Mock(return_value=connected),
        disconnect=AsyncMock(),
        read=AsyncMock(return_value=[1]),
    )
    return hub


@pytest.mark.asyncio
async def test_a_wedged_but_open_link_is_dropped_after_three_unanswered_requests() -> None:
    hub = make_hub()

    for _ in range(2):
        await hub._handle_transport_exception(ModbusTimeoutError("no answer"), "holding read")
    hub._transport.disconnect.assert_not_awaited()

    await hub._handle_transport_exception(ModbusTimeoutError("no answer"), "holding read")

    hub._transport.disconnect.assert_awaited_once()
    assert hub._unanswered_requests == 0


@pytest.mark.asyncio
async def test_a_desynced_stream_counts_towards_dropping_the_link() -> None:
    hub = make_hub()

    for _ in range(3):
        await hub._handle_transport_exception(ModbusDesyncError("out of step"), "holding read")

    hub._transport.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_refused_register_keeps_the_link_and_clears_the_count() -> None:
    hub = make_hub()

    await hub._handle_transport_exception(ModbusTimeoutError("no answer"), "holding read")
    await hub._handle_transport_exception(ModbusTimeoutError("no answer"), "holding read")
    # A device that refuses a register is a device still answering.
    await hub._handle_transport_exception(IllegalDataAddressError(), "holding read")
    await hub._handle_transport_exception(ModbusTimeoutError("no answer"), "holding read")

    hub._transport.disconnect.assert_not_awaited()
    assert hub._unanswered_requests == 1


@pytest.mark.asyncio
async def test_a_read_that_answers_clears_the_count() -> None:
    hub = make_hub()

    await hub._handle_transport_exception(ModbusTimeoutError("no answer"), "holding read")
    await hub._handle_transport_exception(ModbusTimeoutError("no answer"), "holding read")
    await hub._async_read_registers("holding", 1, 0x0, 1)
    await hub._handle_transport_exception(ModbusTimeoutError("no answer"), "holding read")

    hub._transport.disconnect.assert_not_awaited()
    assert hub._unanswered_requests == 1


@pytest.mark.asyncio
async def test_a_lost_connection_still_drops_the_link_at_once() -> None:
    hub = make_hub()

    await hub._handle_transport_exception(ModbusConnectionError("gone"), "holding read")

    hub._transport.disconnect.assert_awaited_once()
    assert hub._unanswered_requests == 0
