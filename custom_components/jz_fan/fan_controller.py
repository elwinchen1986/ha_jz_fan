"""BLE communication core for the XD Smart Fan.

Reimplements the connect / discover / write / notify logic that the original
WeChat mini-program performed with wx.* BLE APIs, using bleak.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.characteristic import BleakGATTCharacteristic
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
    DEFAULT_POLL_INTERVAL,
    INIT_PACKETS,
    NO_CHANGE,
    OFF_VALUE,
    ON_VALUE,
    QUERY_PACKET,
    WRITE_DELAY,
)

_LOGGER = logging.getLogger(__name__)


class FanState:
    """Holds the last known state of the fan, mirrored from notify packets."""

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

    def as_dict(self) -> dict:
        return {
            "power": self.power,
            "gear": self.gear,
            "lr_swing": self.lr_swing,
            "ud_swing": self.ud_swing,
            "manual": self.manual,
            "mode": self.mode,
            "timing": self.timing,
            "light": self.light,
            "trumpet": self.trumpet,
            "available": self.available,
        }


def _decode_toggle(current: bool, raw: int) -> bool:
    """Decode a toggle byte from a notify packet.

    255 (0xFF) -> keep current, 2 -> on, otherwise off.
    Mirrors the mini-program logic: 255==keep, else (raw==2).
    """
    if raw == NO_CHANGE:
        return current
    return raw == 2


def _decode_value(current: int, raw: int) -> int:
    """Decode a numeric byte: 255 -> keep current, else raw."""
    if raw == NO_CHANGE:
        return current
    return raw


class XDFanController:
    """Manages a persistent BLE connection to one XD fan."""

    def __init__(
        self,
        device: BLEDevice,
        address: str,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._device = device
        self._address = address
        self._client: BleakClient | None = None
        self._write_char: BleakGATTCharacteristic | None = None
        self._notify_char: BleakGATTCharacteristic | None = None
        self._lock = asyncio.Lock()
        self.state = FanState()
        self._update_callbacks: list[Callable[[], None]] = []
        self._poll_task: asyncio.Task | None = None
        self._poll_interval: float = poll_interval
        # The running HA event loop, captured on connect. BLE notify
        # callbacks fire on a different thread, so listeners must be
        # dispatched back onto this loop before touching HA state.
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- public API -------------------------------------------------------

    @property
    def address(self) -> str:
        return self._address

    def register_callback(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Register a listener invoked whenever state changes."""
        self._update_callbacks.append(cb)

        def _unsub() -> None:
            if cb in self._update_callbacks:
                self._update_callbacks.remove(cb)

        return _unsub

    def set_device(self, device: BLEDevice) -> None:
        """Refresh the BLEDevice reference (from HA bluetooth updates)."""
        self._device = device

    async def async_connect(self) -> None:
        """Establish connection, discover characteristics, subscribe, init."""
        # Remember the loop we run on so notify callbacks (fired on a
        # separate BLE thread)can be marshalled back safely.
        self._loop = asyncio.get_running_loop()
        async with self._lock:
            if self._client and self._client.is_connected:
                return
            _LOGGER.info("Connecting to XD fan %s", self._address)
            client = await establish_connection(
                BleakClient,
                self._device,
                self._address,
                disconnected_callback=self._on_disconnect,
            )
            self._client = client
            self._discover_characteristics(client)

            if self._notify_char is not None:
                try:
                    await client.start_notify(
                        self._notify_char, self._on_notify
                    )
                    _LOGGER.info(
                        "XD fan subscribed to notify %s",
                        self._notify_char.uuid,
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error(
                        "XD fan start_notify on %s failed: %s",
                        self._notify_char.uuid,
                        err,
                    )
            else:
                _LOGGER.error(
                    "XD fan has no notify characteristic; state will not update"
                )

            self.state.available = True
            self._notify_listeners()

        # Give the device a moment to be ready after the connection is
        # established and notifications are subscribed. The mini-program
        # delayed every write by 666ms, so the query packets below are only
        # sent once the link has settled and the notify subscription is
        # active - otherwise the device's first status frame can be lost.
        await asyncio.sleep(WRITE_DELAY)

        # Send init / query packets. These ask the fan to report its full
        # 15-byte status frame back over the notify characteristic, which is
        # what populates the initial state (echo) after connecting.
        for pkt in INIT_PACKETS:
            await self._write(pkt)

    async def async_disconnect(self) -> None:
        self.stop_polling()
        async with self._lock:
            if self._client and self._client.is_connected:
                try:
                    if self._notify_char is not None:
                        await self._client.stop_notify(self._notify_char)
                except Exception:  # noqa: BLE001
                    pass
                await self._client.disconnect()
            self._client = None
            self.state.available = False
            self._notify_listeners()

    async def async_query_state(self) -> None:
        """Ask the fan to report its full status frame.

        The device answers over the notify characteristic, which keeps Home
        Assistant in sync with changes made physically (buttons or remote).
        Safe to call repeatedly.
        """
        await self._write(QUERY_PACKET)

    def start_polling(self) -> None:
        """Start the periodic status-query loop (idempotent)."""
        if self._poll_interval <= 0:
            return
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.ensure_future(self._poll_loop())

    def stop_polling(self) -> None:
        """Stop the periodic status-query loop."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    def set_poll_interval(self, seconds: float) -> None:
        """Update the poll interval; restart the loop if it is running."""
        self._poll_interval = seconds
        was_running = (
            self._poll_task is not None and not self._poll_task.done()
        )
        if was_running:
            self.stop_polling()
            self.start_polling()

    async def _poll_loop(self) -> None:
        """Periodically query fan status, reconnecting if the link drops."""
        try:
            while True:
                await asyncio.sleep(self._poll_interval)
                try:
                    if not self._client or not self._client.is_connected:
                        # Link dropped (device power-cycled etc.); bring it
                        # back so physical changes stay in sync.
                        await self.async_connect()
                    else:
                        await self.async_query_state()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("XD fan poll iteration failed: %s", err)
        except asyncio.CancelledError:
            pass

    async def async_set_power(self, on: bool) -> None:
        self.state.power = on
        await self._send_field(IDX_POWER, ON_VALUE if on else OFF_VALUE)

    async def async_set_gear(self, gear: int) -> None:
        self.state.gear = gear
        await self._send_field(IDX_GEAR, gear)

    async def async_set_lr_swing(self, on: bool) -> None:
        value = 5 if on else 0
        self.state.lr_swing = value
        await self._send_field(IDX_LR_SWING, value)

    async def async_set_lr_swing_value(self, value: int) -> None:
        """Set the left/right swing to a specific angle step (0..4)."""
        self.state.lr_swing = value
        await self._send_field(IDX_LR_SWING, value)

    async def async_set_ud_swing(self, value: int) -> None:
        self.state.ud_swing = value
        await self._send_field(IDX_UD_SWING, value)

    async def async_set_manual(self, direction: int) -> None:
        """Nudge the fan head in a direction (1=up 2=down 3=left 4=right)."""
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

    # ---- internal helpers -------------------------------------------------

    def _discover_characteristics(self, client: BleakClient) -> None:
        """Find writable and notify characteristics.

        Mirrors the WeChat mini-program logic: it used the 3rd primary
        service (services[2]) and, within that single service, picked the
        characteristic exposing the ``write`` property for writing and the
        ``notify`` / ``indicate`` property for notifications.

        We log every discovered service / characteristic so the actual GATT
        layout can be verified, then apply the same "3rd service" selection
        with a fallback that scans all services if needed.
        """
        services = list(client.services)

        # Log the full GATT layout for troubleshooting.
        for s_idx, service in enumerate(services):
            _LOGGER.info("XD fan service[%d] uuid=%s", s_idx, service.uuid)
            for char in service.characteristics:
                _LOGGER.info(
                    "  char uuid=%s props=%s",
                    char.uuid,
                    ",".join(char.properties),
                )

        write_char = None
        notify_char = None

        def _pick_from_service(service) -> tuple:
            # The mini-program only ever wrote to a characteristic exposing the
            # standard ``write`` (write-with-response) property. Match that
            # exactly: prefer a "write" characteristic; only fall back to a
            # write-without-response one when no response-capable write exists.
            w = w_no_resp = n = None
            for char in service.characteristics:
                props = char.properties
                if w is None and "write" in props:
                    w = char
                if w_no_resp is None and "write-without-response" in props:
                    w_no_resp = char
                if n is None and ("notify" in props or "indicate" in props):
                    n = char
            return (w or w_no_resp), n

        # Primary strategy: the mini-program's primary services[2].
        if len(services) > 2 and getattr(services[2], "is_primary", True):
            write_char, notify_char = _pick_from_service(services[2])
            if write_char is not None:
                _LOGGER.debug(
                    "XD fan using services[2] uuid=%s", services[2].uuid
                )

        # Fallback: scan every service. The mini-program relied on write and
        # notify living on the *same* service, so prefer a service that
        # exposes both - otherwise we may subscribe to the wrong notify
        # characteristic and never receive the device's status echo.
        if write_char is None:
            for service in services:
                w, n = _pick_from_service(service)
                if w is not None and n is not None:
                    write_char, notify_char = w, n
                    _LOGGER.debug(
                        "XD fan fallback service (write+notify) uuid=%s",
                        service.uuid,
                    )
                    break
        # Secondary fallback: any service with a writable characteristic.
        if write_char is None:
            for service in services:
                w, n = _pick_from_service(service)
                if w is not None:
                    write_char = w
                    notify_char = n if n is not None else notify_char
                    _LOGGER.debug(
                        "XD fan fallback service (write only) uuid=%s",
                        service.uuid,
                    )
                    break
        if notify_char is None:
            for service in services:
                _, n = _pick_from_service(service)
                if n is not None:
                    notify_char = n
                    break

        self._write_char = write_char
        self._notify_char = notify_char
        _LOGGER.info(
            "XD fan selected chars: write=%s (props=%s) notify=%s (props=%s)",
            getattr(write_char, "uuid", None),
            ",".join(write_char.properties) if write_char else None,
            getattr(notify_char, "uuid", None),
            ",".join(notify_char.properties) if notify_char else None,
        )
        if write_char is not None and notify_char is not None and \
                write_char.uuid == notify_char.uuid:
            _LOGGER.warning(
                "XD fan write and notify are the same characteristic (%s); "
                "state echo may not arrive - check GATT layout above",
                write_char.uuid,
            )
        if write_char is None:
            raise RuntimeError("No writable characteristic found on XD fan")

    def _build_packet(self, index: int, value: int) -> list[int]:
        """Build a full control packet with only one field changed."""
        payload = [NO_CHANGE] * 10  # bytes 5..14
        payload[index - IDX_POWER] = value
        return CTRL_HEADER + payload

    async def _send_field(self, index: int, value: int) -> None:
        packet = self._build_packet(index, value)
        await self._write(packet)
        self._notify_listeners()

    async def _write(self, packet: list[int]) -> None:
        async with self._lock:
            if not self._client or not self._client.is_connected:
                _LOGGER.warning("Write skipped, XD fan not connected")
                return
            if self._write_char is None:
                _LOGGER.warning("Write skipped, no write characteristic")
                return
            data = bytes(packet)
            # The mini-program used wx.writeBLECharacteristicValue, whose
            # default write type is "write with response". Prefer that when
            # the characteristic supports it, and fall back to
            # write-without-response only when it is the sole option.
            props = self._write_char.properties
            use_response = "write" in props
            if not use_response and "write-without-response" not in props:
                # No standard write property advertised; try with response.
                use_response = True
            _LOGGER.debug(
                "XD fan write (response=%s) to %s: %s",
                use_response,
                self._write_char.uuid,
                data.hex(" "),
            )
            try:
                await self._client.write_gatt_char(
                    self._write_char, data, response=use_response
                )
            except Exception as err:  # noqa: BLE001
                # Some stacks reject the chosen write type; retry the other.
                _LOGGER.debug(
                    "XD fan write failed (response=%s): %s; retrying",
                    use_response,
                    err,
                )
                await self._client.write_gatt_char(
                    self._write_char, data, response=not use_response
                )
        # Pace consecutive writes like the original app.
        await asyncio.sleep(WRITE_DELAY)

    def _on_notify(
        self, _char: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle a notify packet and update state."""
        # Log every incoming frame so users can verify the device is
        # actually pushing status back. This runs on the BLE thread.
        _LOGGER.info(
            "XD fan notify recv (%d bytes): %s",
            len(data),
            bytes(data).hex(" "),
        )
        n = list(data)
        if len(n) < 15:
            _LOGGER.warning(
                "XD fan short notify frame (%d<15), skipping: %s",
                len(n),
                bytes(data).hex(" "),
            )
            return
        s = self.state
        s.power = _decode_toggle(s.power, n[IDX_POWER])
        s.gear = _decode_value(s.gear, n[IDX_GEAR])
        s.lr_swing = _decode_value(s.lr_swing, n[IDX_LR_SWING])
        s.ud_swing = _decode_value(s.ud_swing, n[IDX_UD_SWING])
        s.manual = _decode_value(s.manual, n[IDX_MANUAL])
        s.mode = _decode_value(s.mode, n[IDX_MODE])
        s.timing = _decode_value(s.timing, n[IDX_TIMING])
        s.light = _decode_toggle(s.light, n[IDX_LIGHT])
        s.trumpet = _decode_toggle(s.trumpet, n[IDX_TRUMPET])
        _LOGGER.info("XD fan state updated: %s", s.as_dict())
        self._notify_listeners()

    def _on_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.debug("XD fan %s disconnected", self._address)
        self.state.available = False
        self._notify_listeners()

    def _notify_listeners(self) -> None:
        """Notify listeners on the HA event loop.

        BLE notifications arrive on a background thread, but the listeners
        call ``async_write_ha_state()`` which is *not* thread-safe and must
        run on the event loop. When a loop is known, marshal the callbacks
        onto it with ``call_soon_threadsafe``; otherwise call directly (e.g.
        before the first connection).
        """
        loop = self._loop
        callbacks = list(self._update_callbacks)

        def _run() -> None:
            for cb in callbacks:
                cb()

        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(_run)
        else:
            _run()