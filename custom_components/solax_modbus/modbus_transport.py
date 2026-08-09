"""Transport adapters for native and Home Assistant Core Modbus access.

Every transport speaks the same contract: reads return the register words and
failures raise, using modbus-connection's neutral exception hierarchy even for
the Core Modbus transport, which the library does not own. That keeps one
vocabulary - ``ModbusExceptionError`` for a device that refused a block,
``ModbusTimeoutError`` for silence, ``ModbusConnectionError`` for a dead link -
for the hub's retry, quarantine and reconnect decisions.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Protocol
from weakref import ReferenceType, ref

from homeassistant.core import HomeAssistant
from modbus_connection import (
    ModbusConnection,
    ModbusConnectionError,
    ModbusError,
    ModbusTimeoutError,
    ModbusUnit,
)

_LOGGER = logging.getLogger(__name__)

CORE_CALL_TYPE_REGISTER_HOLDING = "holding"
CORE_CALL_TYPE_REGISTER_INPUT = "input"
CORE_CALL_TYPE_WRITE_REGISTER = "write_register"
CORE_CALL_TYPE_WRITE_REGISTERS = "write_registers"

# One retry, matching the request-level retry the pymodbus client used to do on
# our behalf. modbus-connection deliberately leaves retries to the caller, and
# an inverter that drops a single frame is common enough to be worth one repeat.
REQUEST_RETRIES = 1


def _get_core_hub(hass: HomeAssistant, name: str) -> Any | None:
    """Resolve the optional Core Modbus hub without coupling native startup to it."""
    try:
        from homeassistant.components.modbus import get_hub
    except ImportError:
        return None
    try:
        return get_hub(hass, name)
    except KeyError:
        return None


class ModbusTransport(Protocol):
    """Common interface used by polling, writes and register quarantine."""

    @property
    def endpoint(self) -> str:
        """Return a human-readable transport endpoint."""

    def is_connected(self) -> bool:
        """Return whether the underlying transport is ready."""

    async def connect(self) -> bool:
        """Connect or attach to the underlying transport."""

    async def disconnect(self) -> None:
        """Drop an unusable link; the next request establishes a new one."""

    async def close(self) -> None:
        """Release the underlying transport."""

    async def read(self, register_type: str, unit: int, address: int, count: int) -> list[int]:
        """Read holding or input registers, or raise ``ModbusError``."""

    async def write(self, unit: int, address: int, values: list[int], *, multiple: bool) -> None:
        """Write one or more registers, or raise ``ModbusError``."""


class LibraryModbusTransport:
    """Transport backed by a modbus-connection link shared with other hubs."""

    def __init__(self, connection: ModbusConnection, label: str) -> None:
        self._connection = connection
        self._label = label
        self._units: dict[int, ModbusUnit] = {}

    def _unit(self, unit: int) -> ModbusUnit:
        """Return the handle for one Modbus address on the shared link."""
        if unit not in self._units:
            self._units[unit] = self._connection.for_unit(unit)
        return self._units[unit]

    @property
    def endpoint(self) -> str:
        return self._label

    def is_connected(self) -> bool:
        return self._connection.connected

    async def connect(self) -> bool:
        # Requests connect on demand; this only exists so startup can report a
        # device that is not reachable yet.
        try:
            await self._connection.connect()
        except ModbusError as err:
            _LOGGER.debug("%s: connect failed: %s", self._label, err)
            return False
        return self._connection.connected

    async def disconnect(self) -> None:
        # Recycle a link that is up but unusable. The connection stays usable:
        # the next request opens a new one. Shared with any other hub on this
        # endpoint, which simply reconnects on its next request too.
        await self._connection.disconnect()

    async def close(self) -> None:
        # The link is shared and reference counted; the hub releases its hold
        # through modbus_link.release() instead of closing it here.
        return

    async def _retried(self, operation: str, call: Callable[[], Any]) -> Any:
        """Run one request, repeating it once if the device stayed silent."""
        for attempt in range(REQUEST_RETRIES + 1):
            try:
                return await call()
            except (ModbusTimeoutError, ModbusConnectionError) as err:
                if attempt >= REQUEST_RETRIES:
                    raise
                _LOGGER.debug("%s: %s failed (%s); retrying once", self._label, operation, err)

    async def read(self, register_type: str, unit: int, address: int, count: int) -> list[int]:
        handle = self._unit(unit)
        if register_type == "input":
            return await self._retried("input read", lambda: handle.read_input_registers(address, count))  # type: ignore[no-any-return]
        return await self._retried("holding read", lambda: handle.read_holding_registers(address, count))  # type: ignore[no-any-return]

    async def write(self, unit: int, address: int, values: list[int], *, multiple: bool) -> None:
        handle = self._unit(unit)
        if multiple:
            # Also the "force FC16" path: a single-register write the inverter
            # only accepts as write-multiple.
            await self._retried("register write", lambda: handle.write_registers(address, list(values)))
        else:
            await self._retried("register write", lambda: handle.write_register(address, values[0]))


class CoreModbusTransport:
    """Transport delegated to a Home Assistant Core Modbus hub."""

    def __init__(
        self,
        hass: HomeAssistant,
        core_hub_name: str,
        owner_name: str,
        *,
        hub_getter: Callable[[HomeAssistant, str], Any] = _get_core_hub,
        reconnect_delay: float = 10,
    ) -> None:
        self._hass = hass
        self._core_hub_name = core_hub_name
        self._owner_name = owner_name
        self._hub_getter = hub_getter
        self._reconnect_delay = reconnect_delay
        self._hub_ref: ReferenceType[Any] | None = None
        self._closed = False

    def _hub_closed(self, reference: ReferenceType[Any]) -> None:
        if reference is self._hub_ref:
            self._hub_ref = None

    def _resolve_hub(self) -> Any | None:
        if self._closed:
            return None
        hub = self._hub_ref() if self._hub_ref is not None else None
        if hub is not None:
            return hub
        try:
            hub = self._hub_getter(self._hass, self._core_hub_name)
        except KeyError:
            return None
        if hub is not None:
            self._hub_ref = ref(hub, self._hub_closed)
        return hub

    @staticmethod
    def _config_delay(hub: Any) -> Any:
        return getattr(hub, "config_delay", getattr(hub, "_config_delay", 0))

    @classmethod
    def _hub_is_connected(cls, hub: Any) -> bool:
        client = getattr(hub, "_client", None)
        return bool(client and getattr(client, "connected", False) and not cls._config_delay(hub))

    @property
    def endpoint(self) -> str:
        hub = self._resolve_hub()
        params = getattr(hub, "_pb_params", {}) if hub is not None else {}
        host = params.get("host", "")
        port = params.get("port", "")
        return f"{host}:{port}" if host or port else f"Core Modbus hub '{self._core_hub_name}'"

    def is_connected(self) -> bool:
        hub = self._resolve_hub()
        return bool(hub and self._hub_is_connected(hub))

    async def connect(self) -> bool:
        self._closed = False
        hub = self._resolve_hub()
        if hub is None:
            _LOGGER.warning("CoreModbusHub '%s' not available", self._core_hub_name)
            return False
        if self._hub_is_connected(hub):
            return True

        await asyncio.sleep(self._reconnect_delay)
        hub = self._resolve_hub()
        if hub is not None and self._hub_is_connected(hub):
            return True

        reason = " during its configured startup delay" if hub is not None and self._config_delay(hub) else ""
        _LOGGER.warning("%s: Core Modbus hub '%s' is not ready%s", self._owner_name, self._core_hub_name, reason)
        return False

    async def disconnect(self) -> None:
        # The Core hub owns the client and its reconnect policy.
        return

    async def close(self) -> None:
        # The Core hub owns the shared client. Only release our reference.
        self._closed = True
        self._hub_ref = None

    def _connected_hub(self) -> Any:
        hub = self._resolve_hub()
        if hub is None or not self._hub_is_connected(hub):
            raise ModbusConnectionError(f"Core Modbus hub '{self._core_hub_name}' is not connected")
        return hub

    @staticmethod
    def _checked(response: Any, operation: str) -> Any:
        """Translate a Core hub answer into the neutral contract."""
        # async_pb_call answers None for a failed call and a pymodbus PDU
        # otherwise; neither carries a Modbus exception code we could keep.
        if response is None:
            raise ModbusError(f"Core Modbus {operation} returned no response")
        if response.isError():
            raise ModbusError(f"Core Modbus {operation} was rejected: {response}")
        return response

    async def read(self, register_type: str, unit: int, address: int, count: int) -> list[int]:
        hub = self._connected_hub()
        call_type = CORE_CALL_TYPE_REGISTER_INPUT if register_type == "input" else CORE_CALL_TYPE_REGISTER_HOLDING
        response = self._checked(await hub.async_pb_call(unit, address, count, call_type), f"{register_type} read")
        return list(response.registers)

    async def write(self, unit: int, address: int, values: list[int], *, multiple: bool) -> None:
        hub = self._connected_hub()
        call_type = CORE_CALL_TYPE_WRITE_REGISTERS if multiple else CORE_CALL_TYPE_WRITE_REGISTER
        value: int | list[int] = list(values) if multiple else values[0]
        self._checked(await hub.async_pb_call(unit, address, value, call_type), "write")


class UnavailableModbusTransport:
    """Inert transport used for an invalid interface configuration."""

    def __init__(self, interface: str) -> None:
        self._interface = interface

    @property
    def endpoint(self) -> str:
        return f"unsupported interface '{self._interface}'"

    def is_connected(self) -> bool:
        return False

    async def connect(self) -> bool:
        return False

    async def disconnect(self) -> None:
        return

    async def close(self) -> None:
        return

    async def read(self, register_type: str, unit: int, address: int, count: int) -> list[int]:
        raise ModbusConnectionError(f"unsupported interface '{self._interface}'")

    async def write(self, unit: int, address: int, values: list[int], *, multiple: bool) -> None:
        raise ModbusConnectionError(f"unsupported interface '{self._interface}'")
