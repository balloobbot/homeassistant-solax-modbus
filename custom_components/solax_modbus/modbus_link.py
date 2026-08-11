"""Share one modbus-connection link per physical Modbus endpoint.

A SolaX config entry addresses one inverter, but several entries often sit on
one RS485 bus or one TCP gateway at different Modbus addresses. Before this
module each entry opened its own socket or serial port to that endpoint and the
integration could only warn about it; now they share a single, internally
serialized ``ModbusConnection`` and select their inverter with ``for_unit()``.

Links are keyed by ``ModbusParams.endpoint``, which is deliberately just the
transport, host and port - two entries on one gateway share a link even when
their link settings differ - and reference-counted, so the link closes when the
last hub using it goes away.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from modbus_connection import (
    ModbusConnection,
    ModbusSerialParams,
    ModbusTcpParams,
    ModbusUnit,
)

from .const import (
    CONF_BAUDRATE,
    CONF_INTERFACE,
    CONF_SERIAL_PORT,
    CONF_TCP_TYPE,
    DEFAULT_BAUDRATE,
    DEFAULT_PORT,
    DEFAULT_SERIAL_PORT,
    DEFAULT_TCP_TYPE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# modbus_connection has no public alias for the parameter union - 4.4.0 dropped
# the internal one and spells the union inline - so a consumer that stores "the
# parameters this entry uses" names it itself. SolaX only ever opens TCP sockets
# and serial ports, so this is narrower than the library's four-way union.
ModbusParams = ModbusTcpParams | ModbusSerialParams

DATA_LINKS = "_modbus_links"

# SolaX names its TCP framings after the flavour of Modbus on the wire; the
# library names the plain one after the socket header it carries.
_TCP_FRAMERS = {"tcp": "socket", "rtu": "rtu", "ascii": "ascii"}


def build_params(config: Mapping[str, Any]) -> ModbusParams | None:
    """Build the connection parameters for a config entry, or None for Core Modbus."""
    interface = config.get(CONF_INTERFACE) or ("serial" if config.get("read_serial", False) else "tcp")
    if interface == "tcp":
        host = config.get(CONF_HOST)
        if not host:
            return None
        tcp_type = str(config.get(CONF_TCP_TYPE, DEFAULT_TCP_TYPE))
        framer = _TCP_FRAMERS.get(tcp_type)
        if framer is None:
            _LOGGER.warning("Unknown TCP type %r; falling back to plain Modbus TCP framing", tcp_type)
            framer = "socket"
        return ModbusTcpParams(
            host=str(host),
            port=int(config.get(CONF_PORT, DEFAULT_PORT)),
            framer=framer,  # type: ignore[arg-type]  # validated by _TCP_FRAMERS
        )
    if interface == "serial":
        return ModbusSerialParams(
            device=str(config.get(CONF_SERIAL_PORT, DEFAULT_SERIAL_PORT)),
            baudrate=int(config.get(CONF_BAUDRATE, DEFAULT_BAUDRATE)),
            parity="N",
            stopbits=1,
            bytesize=8,
            framer="rtu",
        )
    return None


def _connection_class(params: ModbusParams) -> type[ModbusConnection]:
    """Return the backend that can actually speak this link.

    Neither backend covers everything SolaX needs, so the choice is per link:

    - ASCII framing over a TCP socket is pymodbus-only; the tmodbus backend
      rejects it at construction.
    - Serial ports are tmodbus-only, because its serialx transport accepts the
      ``esphome-hass://`` URLs the config flow offers for remote RS485 adapters,
      which pyserial cannot open.

    Everything else works on either; tmodbus is the default.
    """
    if isinstance(params, ModbusTcpParams) and params.framer == "ascii":
        from modbus_connection.pymodbus import ModbusConnection as PymodbusConnection

        return PymodbusConnection

    from modbus_connection.tmodbus import ModbusConnection as TmodbusConnection

    return TmodbusConnection


def describe(params: ModbusParams) -> str:
    """Return a short, user-facing description of a link."""
    if isinstance(params, ModbusSerialParams):
        return f"{params.device}@{params.baudrate}"
    return f"{params.host}:{params.port}"


@dataclass
class _SharedLink:
    """One live connection and the hubs holding it open."""

    connection: ModbusConnection
    params: ModbusParams
    users: set[str] = field(default_factory=set)


def _links(hass: HomeAssistant) -> dict[Any, _SharedLink]:
    return hass.data.setdefault(DOMAIN, {}).setdefault(DATA_LINKS, {})  # type: ignore[no-any-return]


def acquire(hass: HomeAssistant, params: ModbusParams, owner: str, *, timeout: float) -> ModbusConnection:
    """Return the shared connection for ``params``, creating it if needed.

    ``owner`` identifies the hub holding the link; releasing the last owner
    closes the connection.
    """
    links = _links(hass)
    link = links.get(params.endpoint)
    if link is None:
        connection = _connection_class(params)(params, timeout=timeout)
        link = links[params.endpoint] = _SharedLink(connection, params)
        _LOGGER.debug("%s: opened a Modbus link to %s", owner, describe(params))
    elif link.params != params:
        _LOGGER.warning(
            "%s and %s address the same Modbus endpoint (%s) with different link settings; using the settings of the first",
            owner,
            ", ".join(sorted(link.users)) or "another inverter",
            describe(params),
        )
    elif owner not in link.users:
        _LOGGER.debug("%s: sharing the Modbus link to %s with %s", owner, describe(params), ", ".join(sorted(link.users)))
    link.users.add(owner)
    return link.connection


async def release(hass: HomeAssistant, params: ModbusParams, owner: str) -> None:
    """Drop ``owner``'s hold on a shared connection, closing the last one out."""
    links = _links(hass)
    link = links.get(params.endpoint)
    if link is None:
        return
    link.users.discard(owner)
    if link.users:
        return
    links.pop(params.endpoint, None)
    _LOGGER.debug("%s: closing the Modbus link to %s", owner, describe(params))
    await link.connection.close()


def unit_for(connection: ModbusConnection, modbus_addr: int) -> ModbusUnit:
    """Return the unit handle for one inverter on a link."""
    return connection.for_unit(modbus_addr)
