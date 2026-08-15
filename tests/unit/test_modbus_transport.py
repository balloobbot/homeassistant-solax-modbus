"""Tests for the modbus-connection and Home Assistant Core Modbus transports."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from homeassistant.exceptions import HomeAssistantError
from modbus_connection import (
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusError,
    ModbusTimeoutError,
)
from modbus_connection.mock import MockModbusConnection

from custom_components.solax_modbus import SolaXModbusHub, block
from custom_components.solax_modbus.const import REGISTER_U16
from custom_components.solax_modbus.modbus_transport import (
    CORE_CALL_TYPE_REGISTER_HOLDING,
    CORE_CALL_TYPE_REGISTER_INPUT,
    CORE_CALL_TYPE_WRITE_REGISTER,
    CORE_CALL_TYPE_WRITE_REGISTERS,
    CoreModbusTransport,
    LibraryModbusTransport,
)


def core_response(registers: list[int] | None = None) -> SimpleNamespace:
    """Build the pymodbus-shaped answer a Core Modbus hub returns."""
    return SimpleNamespace(registers=registers if registers is not None else [], isError=lambda: False)


class FakeCoreHub:
    """Minimal Core Modbus hub exposing its supported call interface."""

    def __init__(self, responses: list[Any] | None = None, *, connected: bool = True, config_delay: int = 0) -> None:
        self._client = SimpleNamespace(connected=connected)
        self.config_delay = config_delay
        self._responses = list(responses or [])
        self.calls: list[tuple[int, int, int | list[int], str]] = []

    async def async_pb_call(self, unit: int, address: int, value: int | list[int], call_type: str) -> Any:
        self.calls.append((unit, address, value, call_type))
        if self._responses:
            return self._responses.pop(0)
        return core_response([0] * (value if isinstance(value, int) else 1))


def make_core_transport(core_hub: FakeCoreHub) -> CoreModbusTransport:
    """Create a Core transport backed by a test hub."""
    return CoreModbusTransport(
        cast(Any, object()),
        "core",
        "test",
        hub_getter=lambda hass, name: core_hub,
        reconnect_delay=0,
    )


def make_hub(transport: Any) -> Any:
    """Build the minimal integration hub state needed by the I/O paths."""
    hub = cast(Any, object.__new__(SolaXModbusHub))
    hub._transport = transport
    hub._name = "test"
    hub._stopping = False
    hub._inflight_tasks = set()
    hub._unanswered_requests = 0
    hub._link_params = None
    hub.plugin = SimpleNamespace(order32="big")
    return hub


def make_quarantine_hub(transport: Any) -> Any:
    """Extend the hub state with what the quarantine probes touch."""
    hub = make_hub(transport)
    hub._modbus_addr = 1
    hub._time_out = 15
    hub.bisect_max_depth = 10
    hub.bad_regs = {"holding": set(), "input": set()}
    hub.initial_groups = {}
    hub.groups = {}
    hub.blocks_changed = False
    hub._comm_last_quarantined_register = None
    hub._comm_last_recovered_register = None
    hub._ensure_quarantine_recheck_task = lambda: None
    hub._update_communication_data = lambda: None
    hub._publish_communication_diagnostics = lambda: None
    return hub


@pytest.fixture
def mock_link() -> tuple[MockModbusConnection, LibraryModbusTransport]:
    """A library transport over the in-memory backend."""
    connection = MockModbusConnection()
    return connection, LibraryModbusTransport(connection, "mock:502")


@pytest.mark.asyncio
async def test_library_transport_reads_and_writes_the_addressed_unit(
    mock_link: tuple[MockModbusConnection, LibraryModbusTransport],
) -> None:
    connection, transport = mock_link
    connection.for_unit(1).holding[10] = [111, 222]
    connection.for_unit(2).input[20] = [1, 2, 3]
    writes: list[Any] = []
    connection.for_unit(3).on_write(writes.append)
    connection.for_unit(4).on_write(writes.append)

    assert await transport.read("holding", unit=1, address=10, count=2) == [111, 222]
    assert await transport.read("input", unit=2, address=20, count=3) == [1, 2, 3]
    await transport.write(unit=3, address=30, values=[7], multiple=False)
    await transport.write(unit=4, address=40, values=[8, 9], multiple=True)

    # A single-register write goes out as FC06 unless the caller forces FC16.
    assert [(event.address, event.values, event.function_code) for event in writes] == [
        (30, [7], 0x06),
        (40, [8, 9], 0x10),
    ]
    assert transport.is_connected() is True
    assert transport.endpoint == "mock:502"


@pytest.mark.asyncio
async def test_forced_multiple_write_of_one_register_uses_fc16(
    mock_link: tuple[MockModbusConnection, LibraryModbusTransport],
) -> None:
    connection, transport = mock_link
    writes: list[Any] = []
    connection.for_unit(1).on_write(writes.append)

    await transport.write(unit=1, address=30, values=[7], multiple=True)

    assert [(event.values, event.function_code) for event in writes] == [([7], 0x10)]


@pytest.mark.asyncio
async def test_timed_out_read_is_retried_once(
    mock_link: tuple[MockModbusConnection, LibraryModbusTransport],
) -> None:
    connection, transport = mock_link
    unit = connection.for_unit(1)
    attempts: list[int] = []

    # fail_read() is permanent, so a transient failure is staged through a
    # register whose value callable raises the first time it is materialized.
    def drops_the_first_frame() -> int:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise ModbusTimeoutError("silence")
        return 42

    unit.holding[9] = drops_the_first_frame

    assert await transport.read("holding", unit=1, address=9, count=1) == [42]
    assert [event.address for event in unit.read_events] == [9, 9]


@pytest.mark.asyncio
async def test_read_timeout_surfaces_after_the_retry(
    mock_link: tuple[MockModbusConnection, LibraryModbusTransport],
) -> None:
    connection, transport = mock_link
    connection.for_unit(1).fail_read(9, ModbusTimeoutError("silence"))
    hub = make_hub(transport)

    with pytest.raises(ModbusTimeoutError):
        await hub.async_read_holding_registers(unit=1, address=9, count=1)

    # A device that stayed silent on an otherwise healthy link keeps the link.
    assert transport.is_connected() is True


@pytest.mark.asyncio
async def test_read_connection_error_drops_the_link(
    mock_link: tuple[MockModbusConnection, LibraryModbusTransport],
) -> None:
    connection, transport = mock_link
    await connection.connect()
    connection.for_unit(1).fail_read(9, ModbusConnectionError("link gone"))
    hub = make_hub(transport)

    with pytest.raises(ModbusConnectionError):
        await hub.async_read_holding_registers(unit=1, address=9, count=1)

    assert transport.is_connected() is False


@pytest.mark.asyncio
async def test_refused_block_reports_the_device_answered(
    mock_link: tuple[MockModbusConnection, LibraryModbusTransport],
) -> None:
    connection, transport = mock_link
    connection.for_unit(1).fail_read(9, IllegalDataAddressError())
    hub = make_quarantine_hub(transport)
    hub.cyclecount = 20
    recorded: list[Any] = []
    hub._record_block_result = lambda *args, **kwargs: recorded.append(kwargs)
    description = SimpleNamespace(key="vpp_status", ignore_readerror=False)
    failing = block(start=9, end=10, descriptions={9: description}, regs=[9])

    result = await hub.async_read_modbus_block({"vpp_status": 5}, failing, "holding")

    assert result.data_succeeded is False
    # The inverter refused the block, so the link itself is fine.
    assert result.communication_succeeded is True
    # An illegal-address answer quarantines at once instead of after a run of failures.
    assert recorded == [{"unreadable": True}]


@pytest.mark.asyncio
async def test_write_failure_is_reported_as_a_home_assistant_error(
    mock_link: tuple[MockModbusConnection, LibraryModbusTransport],
) -> None:
    connection, transport = mock_link
    connection.for_unit(1).fail_write(9, ModbusTimeoutError("silence"))
    hub = make_hub(transport)

    with pytest.raises(HomeAssistantError, match="single-register write failed"):
        await hub.async_lowlevel_write_register(unit=1, address=9, payload=1, register_data_type=REGISTER_U16)

    assert transport.is_connected() is True


@pytest.mark.asyncio
async def test_core_transport_delegates_reads_and_writes_to_core_hub() -> None:
    core_hub = FakeCoreHub()
    transport = make_core_transport(core_hub)

    await transport.read("holding", unit=1, address=10, count=2)
    await transport.read("input", unit=2, address=20, count=3)
    await transport.write(unit=3, address=30, values=[7], multiple=False)
    await transport.write(unit=4, address=40, values=[8, 9], multiple=True)

    assert core_hub.calls == [
        (1, 10, 2, CORE_CALL_TYPE_REGISTER_HOLDING),
        (2, 20, 3, CORE_CALL_TYPE_REGISTER_INPUT),
        (3, 30, 7, CORE_CALL_TYPE_WRITE_REGISTER),
        (4, 40, [8, 9], CORE_CALL_TYPE_WRITE_REGISTERS),
    ]


@pytest.mark.asyncio
async def test_core_transport_reports_failures_in_the_neutral_hierarchy() -> None:
    # A Core hub answers None for a call it could not make.
    transport = make_core_transport(FakeCoreHub([None]))

    with pytest.raises(ModbusError):
        await transport.read("holding", unit=1, address=10, count=2)

    disconnected = make_core_transport(FakeCoreHub(connected=False))
    with pytest.raises(ModbusConnectionError):
        await disconnected.read("holding", unit=1, address=10, count=2)


def test_core_transport_uses_real_core_connection_state() -> None:
    core_hub = FakeCoreHub(connected=True)
    transport = make_core_transport(core_hub)

    assert transport.is_connected() is True

    core_hub.config_delay = 5
    assert transport.is_connected() is False

    core_hub.config_delay = 0
    core_hub._client.connected = False
    assert transport.is_connected() is False


@pytest.mark.asyncio
async def test_core_transport_close_does_not_close_shared_client() -> None:
    core_hub = FakeCoreHub()
    transport = make_core_transport(core_hub)

    assert transport.is_connected() is True
    await transport.close()

    assert core_hub._client.connected is True
    assert transport.is_connected() is False


@pytest.mark.asyncio
async def test_runtime_quarantine_operates_through_core_transport() -> None:
    core_hub = FakeCoreHub([None, None, core_response([0])])
    hub = make_quarantine_hub(make_core_transport(core_hub))
    quarantined: list[int] = []

    async def confirm(typ: str, addr: int) -> bool:
        quarantined.append(addr)
        return True

    hub._confirm_bad_register = confirm
    failing_block = block(start=10, end=12, descriptions={}, regs=[10, 11])

    await hub._runtime_bisect_block(failing_block, "holding", "holding:10-12")

    assert hub.bad_regs["holding"] == {10}
    assert core_hub.calls == [
        (1, 10, 2, CORE_CALL_TYPE_REGISTER_HOLDING),
        (1, 10, 1, CORE_CALL_TYPE_REGISTER_HOLDING),
        (1, 11, 1, CORE_CALL_TYPE_REGISTER_HOLDING),
    ]


@pytest.mark.asyncio
async def test_quarantined_register_is_rechecked_through_core_transport() -> None:
    core_hub = FakeCoreHub()
    hub = make_quarantine_hub(make_core_transport(core_hub))
    hub.bad_regs["holding"].add(10)

    await hub._recheck_quarantined_register("holding", 10)

    assert hub.bad_regs["holding"] == set()
    assert hub.blocks_changed is True
    assert hub._comm_last_recovered_register == "holding 0xa"
    assert core_hub.calls == [(1, 10, 1, CORE_CALL_TYPE_REGISTER_HOLDING)]


@pytest.mark.asyncio
async def test_library_transport_close_leaves_the_shared_link_alone(
    mock_link: tuple[MockModbusConnection, LibraryModbusTransport],
) -> None:
    connection, transport = mock_link
    await connection.connect()

    # The hub releases the link through modbus_link, not through the transport.
    await transport.close()
    after_close = connection.connected

    await transport.disconnect()
    after_disconnect = connection.connected
    # Dropping the link keeps it usable: the next request opens a new one.
    registers = await transport.read("holding", unit=1, address=0, count=1)

    assert (after_close, after_disconnect, connection.connected) == (True, False, True)
    assert registers == [0]
