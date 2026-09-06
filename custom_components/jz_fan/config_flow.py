"""Config flow for the XD Smart Fan (BLE) integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_ADDRESS

from .const import (
    CONF_NAME,
    DOMAIN,
)

# Advertised name fragments seen on XD fans (BT2G / model suffix like F008).
# Used only to sort likely-matching devices to the top; users may still pick
# any nearby device.
_NAME_HINTS = ("BT2G", "F008", "F018", "F016", "XD")


def _looks_like_xd(name: str | None) -> bool:
    if not name:
        return False
    upper = name.upper()
    return any(hint in upper for hint in _NAME_HINTS)


class XDFanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for XD Smart Fan."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, str] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a flow initialized by bluetooth discovery."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovered = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a single discovered device."""
        assert self._discovered is not None
        info = self._discovered
        if user_input is not None:
            return self.async_create_entry(
                title=info.name or info.address,
                data={
                    CONF_ADDRESS: info.address,
                    CONF_NAME: info.name or info.address,
                },
            )

        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": info.name or info.address,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user-initiated flow:pick from discovered devices."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            name = self._discovered_devices.get(address, address)
            return self.async_create_entry(
                title=name,
                data={CONF_ADDRESS: address, CONF_NAME: name},
            )

        current_addresses = self._async_current_ids()
        devices: list[tuple[str, str]] = []
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in current_addresses:
                continue
            if info.address in self._discovered_devices:
                continue
            name = info.name or info.address
            self._discovered_devices[info.address] = name
            devices.append((info.address, name))

        if not devices:
            return self.async_abort(reason="no_devices_found")

        # Sort likely XD devices first for convenience.
        devices.sort(key=lambda d: (not _looks_like_xd(d[1]), d[1]))

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {addr: f"{name} ({addr})" for addr, name in devices}
                    )
                }
            ),
        )