"""The XD Smart Fan (BLE) integration."""
from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_ADDRESS,
    DOMAIN,
)
from .fan_controller import XDFanController

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.FAN,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.BUTTON,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up XD Smart Fan from a config entry."""
    address: str = entry.data[CONF_ADDRESS]

    ble_device = bluetooth.async_ble_device_from_address(
        hass, address.upper(), connectable=True
    )
    if ble_device is None:
        raise ConfigEntryNotReady(
            f"Could not find XD fan with address {address}"
        )

    controller = XDFanController(ble_device, address)

    try:
        await controller.async_connect()
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(
            f"Could not connect to XD fan {address}: {err}"
        ) from err

    # Track advertisements so we can refresh the BLEDevice reference and
    # reconnect if the device drops off and comes back.
    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _make_ble_update_callback(controller),
            bluetooth.BluetoothCallbackMatcher(
                address=address.upper(), connectable=True
            ),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _make_ble_update_callback(controller: XDFanController):
    """Build a bluetooth advertisement callback bound to a controller."""

    def _callback(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        controller.set_device(service_info.device)

    return _callback


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unload_ok:
        controller: XDFanController = hass.data[DOMAIN].pop(entry.entry_id)
        await controller.async_disconnect()
    return unload_ok