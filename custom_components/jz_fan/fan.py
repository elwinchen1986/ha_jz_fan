"""Fan platform for the XD Smart Fan (BLE) integration."""
from __future__ import annotations

import math
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from .const import (
    CONF_NAME,
    DOMAIN,
    MAX_GEAR,
    MIN_GEAR,
    PRESET_MODES,
    PRESET_MODES_REVERSE,
)
from .fan_controller import XDFanController

SPEED_RANGE = (MIN_GEAR, MAX_GEAR)  # 1..12


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the XD fan entity from a config entry."""
    controller: XDFanController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([XDFan(controller, entry)])


class XDFan(FanEntity):
    """Representation of an XD BLE fan."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_translation_key = "fan"
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.OSCILLATE
        | FanEntityFeature.PRESET_MODE
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    _attr_preset_modes = list(PRESET_MODES.keys())
    _attr_speed_count = MAX_GEAR

    def __init__(
        self, controller: XDFanController, entry: ConfigEntry
    ) -> None:
        self._controller = controller
        self._attr_unique_id = f"{controller.address}_fan"
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
        return self._controller.state.power

    @property
    def percentage(self) -> int | None:
        if not self._controller.state.power:
            return 0
        return ranged_value_to_percentage(
            SPEED_RANGE, self._controller.state.gear
        )

    @property
    def oscillating(self) -> bool:
        return self._controller.state.lr_swing > 0

    @property
    def preset_mode(self) -> str | None:
        return PRESET_MODES_REVERSE.get(self._controller.state.mode)

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        await self._controller.async_set_power(True)
        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
        if percentage is not None:
            await self.async_set_percentage(percentage)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._controller.async_set_power(False)

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage == 0:
            await self._controller.async_set_power(False)
            return
        if not self._controller.state.power:
            await self._controller.async_set_power(True)
        gear = math.ceil(
            percentage_to_ranged_value(SPEED_RANGE, percentage)
        )
        gear = max(MIN_GEAR, min(MAX_GEAR, gear))
        await self._controller.async_set_gear(gear)

    async def async_oscillate(self, oscillating: bool) -> None:
        await self._controller.async_set_lr_swing(oscillating)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in PRESET_MODES:
            return
        await self._controller.async_set_mode(PRESET_MODES[preset_mode])