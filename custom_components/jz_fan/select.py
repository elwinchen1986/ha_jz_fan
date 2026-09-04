"""Select platform for the XD Smart Fan (BLE) integration.

Exposes the multi-step angle / timer fields found in the control packet:
left-right swing angle, up-down swing angle and the sleep timer. These
mirror the picker controls in the original WeChat mini-program.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.select import (
    SelectEntity,
    SelectEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_NAME,
    DOMAIN,
    LR_SWING_OPTIONS,
    LR_SWING_OPTIONS_REVERSE,
    UD_SWING_OPTIONS,
    UD_SWING_OPTIONS_REVERSE,
)
from .fan_controller import FanState, XDFanController


def _timing_option_to_value(option: str) -> int:
    return 0 if option == "off" else int(option)


def _timing_value_to_option(value: int) -> str:
    return "off" if value == 0 else str(value)


@dataclass(frozen=True, kw_only=True)
class XDSelectDescription(SelectEntityDescription):
    """Describes an XD fan select entity."""

    current_fn: Callable[[FanState], str]
    select_fn: Callable[[XDFanController, str], Awaitable[None]]


SELECTS: tuple[XDSelectDescription, ...] = (
    XDSelectDescription(
        key="lr_swing",
        translation_key="lr_swing",
        options=list(LR_SWING_OPTIONS.keys()),
        current_fn=lambda s: LR_SWING_OPTIONS_REVERSE.get(s.lr_swing, "off"),
        select_fn=lambda c, o: c.async_set_lr_swing_value(LR_SWING_OPTIONS[o]),
    ),
    XDSelectDescription(
        key="ud_swing",
        translation_key="ud_swing",
        options=list(UD_SWING_OPTIONS.keys()),
        current_fn=lambda s: UD_SWING_OPTIONS_REVERSE.get(s.ud_swing, "off"),
        select_fn=lambda c, o: c.async_set_ud_swing(UD_SWING_OPTIONS[o]),
    ),
    XDSelectDescription(
        key="timing",
        translation_key="timing",
        options=["off"] + [str(i) for i in range(1, 13)],
        current_fn=lambda s: _timing_value_to_option(s.timing),
        select_fn=lambda c, o: c.async_set_timing(_timing_option_to_value(o)),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up XD fan select entities from a config entry."""
    controller: XDFanController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        XDSelect(controller, entry, desc) for desc in SELECTS
    )


class XDSelect(SelectEntity):
    """A multi-step field exposed by the XD fan control packet."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: XDSelectDescription

    def __init__(
        self,
        controller: XDFanController,
        entry: ConfigEntry,
        description: XDSelectDescription,
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
    def current_option(self) -> str | None:
        return self.entity_description.current_fn(self._controller.state)

    async def async_select_option(self, option: str) -> None:
        await self.entity_description.select_fn(self._controller, option)