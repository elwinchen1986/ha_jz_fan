"""Button platform for the XD Smart Fan (BLE) integration.

Exposes the manual head-nudge controls (up / down / left / right) found in
the control packet. These are momentary actions, matching the directional
buttons in the original WeChat mini-program.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import (
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_NAME,
    DOMAIN,
    MANUAL_DOWN,
    MANUAL_LEFT,
    MANUAL_RIGHT,
    MANUAL_UP,
)
from .fan_controller import XDFanController


@dataclass(frozen=True, kw_only=True)
class XDButtonDescription(ButtonEntityDescription):
    """Describes an XD fan manual-direction button."""

    press_fn: Callable[[XDFanController], Awaitable[None]]


BUTTONS: tuple[XDButtonDescription, ...] = (
    XDButtonDescription(
        key="manual_up",
        translation_key="manual_up",
        press_fn=lambda c: c.async_set_manual(MANUAL_UP),
    ),
    XDButtonDescription(
        key="manual_down",
        translation_key="manual_down",
        press_fn=lambda c: c.async_set_manual(MANUAL_DOWN),
    ),
    XDButtonDescription(
        key="manual_left",
        translation_key="manual_left",
        press_fn=lambda c: c.async_set_manual(MANUAL_LEFT),
    ),
    XDButtonDescription(
        key="manual_right",
        translation_key="manual_right",
        press_fn=lambda c: c.async_set_manual(MANUAL_RIGHT),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up XD fan manual-direction buttons from a config entry."""
    controller: XDFanController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        XDButton(controller, entry, desc) for desc in BUTTONS
    )


class XDButton(ButtonEntity):
    """A momentary manual-direction control on the XD fan."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: XDButtonDescription

    def __init__(
        self,
        controller: XDFanController,
        entry: ConfigEntry,
        description: XDButtonDescription,
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

    @property
    def available(self) -> bool:
        return self._controller.state.available

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self._controller)