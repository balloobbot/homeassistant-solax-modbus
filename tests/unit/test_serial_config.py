"""Regression tests for serial config compatibility."""

import json
from pathlib import Path
from typing import Any

from homeassistant.helpers import selector

from custom_components.solax_modbus.config_flow import SERIAL_SCHEMA
from custom_components.solax_modbus.const import CONF_SERIAL_PORT


def serial_port_validator() -> Any:
    """Return the validator assigned to the existing serial port key."""
    for marker, validator in SERIAL_SCHEMA.schema.items():
        if marker.schema == CONF_SERIAL_PORT:
            return validator
    raise AssertionError("Serial port field is missing")


def test_existing_serial_port_key_accepts_local_path_and_proxy_url() -> None:
    """Changing the UI selector does not change persisted config data."""
    validator = serial_port_validator()

    assert validator("/dev/serial/by-id/existing") == "/dev/serial/by-id/existing"
    proxy_url = "esphome-hass://esphome/entry-id?port_name=Inverter%20RS485"
    assert validator(proxy_url) == proxy_url


def test_current_home_assistant_uses_serial_port_selector() -> None:
    """Use HA's discovered-port selector whenever the installed HA supports it."""
    if hasattr(selector, "SerialPortSelector"):
        assert isinstance(serial_port_validator(), selector.SerialPortSelector)


def test_manifest_loads_usb_and_both_modbus_backends() -> None:
    """The selector and both connection backends declare their dependencies.

    Serial ports need tmodbus (its serialx transport opens the proxy URLs the
    selector offers) and ASCII-over-TCP needs pymodbus, so the integration
    installs modbus-connection with both extras.
    """
    manifest_path = Path(__file__).parents[2] / "custom_components" / "solax_modbus" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())

    assert "usb" in manifest["dependencies"]
    assert "modbus-connection[pymodbus,tmodbus]==4.4.0" in manifest["requirements"]
