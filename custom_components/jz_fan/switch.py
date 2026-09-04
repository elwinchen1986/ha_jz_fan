"""Switch platform for the XD Smart Fan (BLE) integration.

Exposes the auxiliary toggles found in the control packet: indicator
light and buzzer/trumpet.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import (
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_NAME, DOMAIN
from .fan_controller import FanState, XDFanController


@dataclass(frozen=True, kw_only=True)
class XDSwitchDescription(SwitchEntityDescription):
    """Describes an XD fan switch."""

    value_fn: Callable[[FanState], bool]
    set_fn: Callable[[XDFanController, bool], Awaitable[None]]


SWITCHES: tuple[XDSwitchDescription, ...] = (
    XDSwitchDescription(
        key="light",
        translation_key="light",
        value_fn=lambda s: s.light,
        set_fn=lambda c, v: c.async_set_light(v),
    ),
    XDSwitchDescription(
        key="trumpet",
        translation_key="trumpet",
        value_fn=lambda s: s.trumpet,
        set_fn=lambda c, v: c.async_set_trumpet(v),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up XD fan switches from a config entry."""
    controller: XDFanController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        XDSwitch(controller, entry, desc) for desc in SWITCHES
    )


class XDSwitch(SwitchEntity):
    """A toggle exposed by the XD fan control packet."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: XDSwitchDescription

    def __init__(
        self,
        controller: XDFanController,
        entry: ConfigEntry,
        description: XDSwitchDescription,
    ) -> None:
        self._controller = controller
        self.entity_description = description
        self._attr_unique_id = f"{controller.address}_{description.key}"
        name = entry.data.get(CONF_NAME, controller.address)
        self._attr_device_info = DeviceInfo(
            connections={("bluetooth", controller.address)},
            identifiers={(DOMAIN, controller.address)},
            manufacturer="XD",
            name=name,
            model="BLE Smart Fan",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._controller.register_callback(self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._controller.state.available

    @property
    def is_on(self) -> bool:
        return self.entity_description.value_fn(self._controller.state)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.entity_description.set_fn(self._controller, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.entity_description.set_fn(self._controller, False)