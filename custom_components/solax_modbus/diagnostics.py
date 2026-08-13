"""Diagnostics support for the SolaX Modbus integration."""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return the last poll's outcome plus the raw register map.

    The fields are built by hand rather than redacted from the entry: the host
    and the serial number never belong in a payload that gets pasted into an
    issue.
    """
    name = entry.options.get(CONF_NAME) or entry.data[CONF_NAME]
    hub = hass.data[DOMAIN][name]["hub"]

    return {
        "inverter": {
            "plugin": getattr(hub.plugin, "plugin_name", None),
            "manufacturer": getattr(hub.plugin, "plugin_manufacturer", None),
            "inverter_type": hub.invertertype,
        },
        "poll": {
            "health": hub.data.get("communication_health"),
            "failed_blocks": hub.communication_failed_blocks(),
            **hub.communication_health_attributes(),
        },
        "quarantine": hub.communication_quarantine_attributes(),
        "registers": await hub.async_read_raw(),
    }
