"""Tests for building and sharing modbus-connection links."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from modbus_connection import ModbusSerialParams, ModbusTcpParams
from modbus_connection.tmodbus import ModbusConnection as TmodbusConnection

from custom_components.solax_modbus import modbus_link
from custom_components.solax_modbus.const import (
    CONF_BAUDRATE,
    CONF_INTERFACE,
    CONF_SERIAL_PORT,
    CONF_TCP_TYPE,
    DOMAIN,
)


def fake_hass() -> Any:
    return cast(Any, SimpleNamespace(data={}))


def test_tcp_type_maps_onto_the_library_framer() -> None:
    base = {CONF_INTERFACE: "tcp", CONF_HOST: "10.0.0.5", CONF_PORT: 502}

    assert modbus_link.build_params({**base, CONF_TCP_TYPE: "tcp"}) == ModbusTcpParams(host="10.0.0.5", port=502, framer="socket")
    assert modbus_link.build_params({**base, CONF_TCP_TYPE: "rtu"}) == ModbusTcpParams(host="10.0.0.5", port=502, framer="rtu")
    assert modbus_link.build_params({**base, CONF_TCP_TYPE: "ascii"}) == ModbusTcpParams(host="10.0.0.5", port=502, framer="ascii")


def test_serial_port_keeps_the_configured_line_settings() -> None:
    params = modbus_link.build_params(
        {
            CONF_INTERFACE: "serial",
            CONF_SERIAL_PORT: "esphome-hass://esphome/entry-id?port_name=Inverter",
            CONF_BAUDRATE: "19200",
        }
    )

    assert params == ModbusSerialParams(
        device="esphome-hass://esphome/entry-id?port_name=Inverter",
        baudrate=19200,
        parity="N",
        stopbits=1,
        bytesize=8,
        framer="rtu",
    )


def test_core_modbus_and_incomplete_tcp_configs_have_no_link() -> None:
    assert modbus_link.build_params({CONF_INTERFACE: "core"}) is None
    assert modbus_link.build_params({CONF_INTERFACE: "tcp"}) is None


def test_every_link_type_rides_the_tmodbus_backend() -> None:
    # Since 4.6.0 tmodbus carries ASCII-over-TCP too, so the pymodbus fallback
    # (and its extra) are gone; every link build_params can produce must
    # construct on tmodbus.
    for params in (
        ModbusTcpParams(host="10.0.0.5"),
        ModbusTcpParams(host="10.0.0.5", framer="rtu"),
        ModbusTcpParams(host="10.0.0.5", framer="ascii"),
        ModbusSerialParams(device="/dev/ttyUSB0"),
    ):
        TmodbusConnection(params)

    ascii_link = modbus_link.acquire(fake_hass(), ModbusTcpParams(host="10.0.0.5", framer="ascii"), "inverter", timeout=5)
    assert isinstance(ascii_link, TmodbusConnection)


@pytest.mark.asyncio
async def test_inverters_on_one_gateway_share_a_single_link() -> None:
    hass = fake_hass()
    first = ModbusTcpParams(host="10.0.0.5", port=502)
    second = ModbusTcpParams(host="10.0.0.5", port=502)

    one = modbus_link.acquire(hass, first, "inverter a", timeout=5)
    two = modbus_link.acquire(hass, second, "inverter b", timeout=5)

    assert one is two
    # Each inverter still addresses its own Modbus unit on that link.
    assert one.for_unit(1) is not one.for_unit(2)

    await modbus_link.release(hass, first, "inverter a")
    assert hass.data[DOMAIN][modbus_link.DATA_LINKS], "the link is still held by inverter b"

    await modbus_link.release(hass, second, "inverter b")
    assert hass.data[DOMAIN][modbus_link.DATA_LINKS] == {}


@pytest.mark.asyncio
async def test_different_endpoints_get_their_own_links() -> None:
    hass = fake_hass()
    lan = ModbusTcpParams(host="10.0.0.5", port=502)
    serial = ModbusSerialParams(device="/dev/ttyUSB0")

    assert modbus_link.acquire(hass, lan, "a", timeout=5) is not modbus_link.acquire(hass, serial, "b", timeout=5)
    assert len(hass.data[DOMAIN][modbus_link.DATA_LINKS]) == 2


@pytest.mark.asyncio
async def test_conflicting_link_settings_are_reported(caplog: Any) -> None:
    hass = fake_hass()
    plain = ModbusTcpParams(host="10.0.0.5", port=502)
    rtu = ModbusTcpParams(host="10.0.0.5", port=502, framer="rtu")

    modbus_link.acquire(hass, plain, "inverter a", timeout=5)
    modbus_link.acquire(hass, rtu, "inverter b", timeout=5)

    assert "different link settings" in caplog.text


@pytest.mark.asyncio
async def test_releasing_an_unknown_link_is_harmless() -> None:
    hass = fake_hass()
    await modbus_link.release(hass, ModbusTcpParams(host="10.0.0.5"), "nobody")


@pytest.mark.asyncio
async def test_two_hubs_on_one_gateway_end_up_on_one_link() -> None:
    """The whole point: two inverters behind one gateway, one socket."""
    from types import ModuleType

    from custom_components.solax_modbus import SolaXModbusHub
    from custom_components.solax_modbus.const import CONF_MODBUS_ADDR, plugin_base

    hass = fake_hass()
    plugin_module = cast(ModuleType, SimpleNamespace(plugin_instance=plugin_base("p", "m", [], [], [], [], [], [])))

    def hub(name: str, modbus_addr: int) -> Any:
        entry = SimpleNamespace(
            options={
                CONF_NAME: name,
                CONF_INTERFACE: "tcp",
                CONF_HOST: "10.0.0.5",
                CONF_PORT: 502,
                CONF_MODBUS_ADDR: modbus_addr,
            }
        )
        return SolaXModbusHub(hass, plugin_module, cast(Any, entry))

    first = hub("Inverter 1", 1)
    second = hub("Inverter 2", 2)

    assert len(hass.data[DOMAIN][modbus_link.DATA_LINKS]) == 1
    assert first._transport._connection is second._transport._connection
    assert first._transport._unit(1) is not second._transport._unit(2)

    await first._async_release_transport()
    assert hass.data[DOMAIN][modbus_link.DATA_LINKS], "inverter 2 still holds the link"
    await second._async_release_transport()
    assert hass.data[DOMAIN][modbus_link.DATA_LINKS] == {}
