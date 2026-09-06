"""BLE communication core for the XD Smart Fan."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection

from .const import (
    CTRL_HEADER,
    IDX_GEAR,
    IDX_LIGHT,
    IDX_LR_SWING,
    IDX_MANUAL,
    IDX_MODE,
    IDX_POWER,
    IDX_TIMING,
    IDX_TRUMPET,
    IDX_UD_SWING,
    NO_CHANGE,
    OFF_VALUE,
    ON_VALUE,
    WRITE_DELAY,
)

_LOGGER = logging.getLogger(__name__)


class FanState:
    """Best-effort state mirror. Notify path is currently silent on Linux."""

    __slots__ = (
        "power", "gear", "lr_swing", "ud_swing", "manual", "mode",
        "timing", "light", "trumpet", "available",
    )

    def __init__(self) -> None:
        self.power: bool = False
        self.gear: int = 1
        self.lr_swing: int = 0
        self.ud_swing: int = 0
        self.manual: int = 0
        self.mode: int = 1
        self.timing: int = 0
        self.light: bool = False
        self.trumpet: bool = False
        self.available: bool = False


class XDFanController:
    """Persistent BLE connection to one XD fan (write-only on Linux)."""

    def __init__(self, device: BLEDevice, address: str) -> None:
        self._device = device
        self._address = address
        self._client: BleakClient | None = None
        self._write_char: BleakGATTCharacteristic | None = None
        self._notify_char: BleakGATTCharacteristic | None = None
        self._lock = asyncio.Lock()
        self.state = FanState()
        self._update_callbacks: list[Callable[[], None]] = []

    @property
    def address(self) -> str:
        return self._address

    def register_callback(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._update_callbacks.append(cb)

        def _unsub() -> None:
            if cb in self._update_callbacks:
                self._update_callbacks.remove(cb)

        return _unsub

    def set_device(self, device: BLEDevice) -> None:
        self._device = device

    async def async_connect(self) -> None:
        """Establish connection, discover chars, subscribe (best-effort)."""
        try:
            async with self._lock:
                if self._client and self._client.is_connected:
                    return
                client = await establish_connection(
                    BleakClient,
                    self._device,
                    self._address,
                    disconnected_callback=self._on_disconnect,
                )
                self._client = client
                await self._await_services_resolved(client)
                self._discover_characteristics(client)
                if self._notify_char is not None:
                    try:
                        await client.start_notify(self._notify_char, self._on_notify)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("start_notify failed: %s", err)
                self.state.available = True
                self._notify_listeners()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("XD fan connect failed (non-fatal): %s", err)

    async def _await_services_resolved(
        self, client: BleakClient, timeout: float = 10.0, interval: float = 0.25
    ) -> None:
        elapsed = 0.0
        while elapsed < timeout:
            try:
                if list(client.services):
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(interval)
            elapsed += interval

    def _discover_characteristics(self, client: BleakClient) -> None:
        """Pick the first service that has both write and notify chars."""
        for service in client.services:
            write_char = None
            notify_char = None
            for char in service.characteristics:
                if write_char is None and (
                    "write" in char.properties
                    or "write-without-response" in char.properties
                ):
                    write_char = char
                if notify_char is None and (
                    "notify" in char.properties or "indicate" in char.properties
                ):
                    notify_char = char
            if write_char is not None and notify_char is not None:
                self._write_char = write_char
                self._notify_char = notify_char
                return
            if write_char is not None and self._write_char is None:
                self._write_char = write_char
        if self._write_char is None:
            raise RuntimeError("No writable characteristic found on XD fan")

    async def async_disconnect(self) -> None:
        async with self._lock:
            if self._client and self._client.is_connected:
                if self._notify_char is not None:
                    try:
                        await self._client.stop_notify(self._notify_char)
                    except Exception:  # noqa: BLE001
                        pass
                await self._client.disconnect()
            self._client = None
            self.state.available = False
            self._notify_listeners()

    async def async_set_power(self, on: bool) -> None:
        self.state.power = on
        await self._send_field(IDX_POWER, ON_VALUE if on else OFF_VALUE)

    async def async_set_gear(self, gear: int) -> None:
        self.state.gear = gear
        await self._send_field(IDX_GEAR, gear)

    async def async_set_lr_swing(self, on: bool) -> None:
        value = 4 if on else 0
        self.state.lr_swing = value
        await self._send_field(IDX_LR_SWING, value)

    async def async_set_lr_swing_value(self, value: int) -> None:
        self.state.lr_swing = value
        await self._send_field(IDX_LR_SWING, value)

    async def async_set_ud_swing(self, value: int) -> None:
        self.state.ud_swing = value
        await self._send_field(IDX_UD_SWING, value)

    async def async_set_manual(self, direction: int) -> None:
        self.state.manual = direction
        await self._send_field(IDX_MANUAL, direction)

    async def async_set_mode(self, mode: int) -> None:
        self.state.mode = mode
        await self._send_field(IDX_MODE, mode)

    async def async_set_timing(self, hours: int) -> None:
        self.state.timing = hours
        await self._send_field(IDX_TIMING, hours)

    async def async_set_light(self, on: bool) -> None:
        self.state.light = on
        await self._send_field(IDX_LIGHT, ON_VALUE if on else OFF_VALUE)

    async def async_set_trumpet(self, on: bool) -> None:
        self.state.trumpet = on
        await self._send_field(IDX_TRUMPET, ON_VALUE if on else OFF_VALUE)

    def _build_packet(self, index: int, value: int) -> list[int]:
        payload = [NO_CHANGE] * 10
        payload[index - IDX_POWER] = value
        return CTRL_HEADER + payload

    async def _send_field(self, index: int, value: int) -> None:
        await self._write(self._build_packet(index, value))
        self._notify_listeners()

    async def _write(self, packet: list[int]) -> None:
        async with self._lock:
            if not self._client or not self._client.is_connected:
                _LOGGER.warning("Write skipped, XD fan not connected")
                return
            if self._write_char is None:
                _LOGGER.warning("Write skipped, no write characteristic")
                return
            use_response = "write" in self._write_char.properties
            try:
                await self._client.write_gatt_char(
                    self._write_char, bytes(packet), response=use_response
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("XD fan write failed (response=%s): %s", use_response, err)
        await asyncio.sleep(WRITE_DELAY)

    def _on_notify(self, char: BleakGATTCharacteristic, data: bytearray) -> None:
        """Hook for future encrypted-link notify decoding. No-op on Linux."""
        if not data:
            return
        _LOGGER.debug("XD fan notify received (%d bytes) on %s", len(data), char.uuid)

    def _on_disconnect(self, _client: BleakClient) -> None:
        self._client = None
        self.state.available = False
        self._notify_listeners()

    def _notify_listeners(self) -> None:
        for cb in list(self._update_callbacks):
            cb()