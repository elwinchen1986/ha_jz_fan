"""BLE communication core for the XD Smart Fan.

Reimplements the connect / discover / write / notify logic that the original
control app performed, using bleak. Kept intentionally close to the known-good
v1.1.6 baseline, with three additions:

  * ``poll_interval`` constructor argument + ``start_polling`` background task
    so state changed physically (buttons / remote) is reflected back into HA.
  * ``async_query_state`` - sends the query packet that asks the fan to report
    its full status frame over the notify characteristic.
  * ``_await_services_resolved`` - waits for the GATT service list to resolve
    before discovering characteristics (BlueZ can report an empty list right
    after connecting).
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
    DEFAULT_POLL_INTERVAL,
    IDX_GEAR,
    IDX_LIGHT,
    IDX_LR_SWING,
    IDX_MANUAL,
    IDX_MODE,
    IDX_POWER,
    IDX_TIMING,
    IDX_TRUMPET,
    IDX_UD_SWING,
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
        poll_interval: int = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._device = device
        self._address = address
        self._poll_interval = poll_interval
        self._client: BleakClient | None = None
        self._write_char: BleakGATTCharacteristic | None = None
        self._notify_char: BleakGATTCharacteristic | None = None
        # During connection we probe every candidate service that exposes a
        # notify characteristic. ``_candidates`` holds (write, notify) pairs
        # per service; the first pair to deliver a valid status frame is
        # locked in as the real command channel (``_channel_locked``).
        self._candidates: list[tuple] = []
        self._notify_chars: list[BleakGATTCharacteristic] = []
        self._channel_locked = False
        self._lock = asyncio.Lock()
        self.state = FanState()
        self._update_callbacks: list[Callable[[], None]] = []
        self._poll_task: asyncio.Task | None = None

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
        async with self._lock:
            if self._client and self._client.is_connected:
                return
            _LOGGER.debug("Connecting to XD fan %s", self._address)
            client = await establish_connection(
                BleakClient,
                self._device,
                self._address,
                disconnected_callback=self._on_disconnect,
            )
            self._client = client

            # BlueZ can report an empty service list immediately after the
            # link comes up; wait for resolution before discovering.
            await self._await_services_resolved(client)
            self._discover_characteristics(client)

            # Subscribe to every candidate notify characteristic; the one
            # that actually delivers a status frame will lock in the real
            # command channel (see ``_on_notify``).
            for notify_char in self._notify_chars:
                try:
                    await client.start_notify(notify_char, self._on_notify)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "XD fan start_notify failed for %s: %s",
                        notify_char.uuid,
                        err,
                    )

            self.state.available = True
            self._notify_listeners()

        # Let the link settle after subscribing, matching the original app's
        # 666ms write pacing, so the first status frame is not lost.
        await asyncio.sleep(WRITE_DELAY)

        # Probe every candidate: send the init/handshake + query packets on
        # each candidate's write characteristic. Only the correct service
        # will reply over notify, which locks the channel. Once locked we
        # stop probing the others.
        if self._candidates and not self._channel_locked:
            for write_char, _notify in self._candidates:
                if self._channel_locked:
                    break
                for pkt in INIT_PACKETS:
                    await self._write(pkt, char=write_char)
                # Give the device a moment to answer before trying the next
                # candidate service.
                await asyncio.sleep(WRITE_DELAY)
        else:
            # Init / query packets ask the fan to report its full 15-byte
            # status frame over notify, populating the initial (echo) state.
            for pkt in INIT_PACKETS:
                await self._write(pkt)

    def start_polling(self) -> None:
        """Start the background task that keeps state in sync and reconnects."""
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.ensure_future(self._poll_loop())

    async def async_query_state(self) -> None:
        """Ask the fan to report its full status frame over notify."""
        await self._write(QUERY_PACKET)

    async def _await_services_resolved(
        self, client: BleakClient, timeout: float = 10.0, interval: float = 0.25
    ) -> None:
        """Wait until the GATT service list is populated."""
        elapsed = 0.0
        while elapsed < timeout:
            try:
                if list(client.services):
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(interval)
            elapsed += interval
        _LOGGER.debug("XD fan services not resolved within %ss", timeout)

    async def _poll_loop(self) -> None:
        """Periodically ensure the link is up and query the current state."""
        while True:
            try:
                if not self._client or not self._client.is_connected:
                    await self.async_connect()
                else:
                    await self.async_query_state()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("XD fan poll cycle failed: %s", err)
            await asyncio.sleep(self._poll_interval)

    async def async_disconnect(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        async with self._lock:
            if self._client and self._client.is_connected:
                for notify_char in self._notify_chars or [self._notify_char]:
                    if notify_char is None:
                        continue
                    try:
                        await self._client.stop_notify(notify_char)
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
        """Discover writable/notify characteristics on every private service.

        The device exposes several private services (aa01/ee01/ae00/dd01),
        each with its own write + notify characteristics. Across platforms the
        service enumeration order differs (BlueZ vs. the original app), so we
        cannot rely on a fixed index to pick the right one. Instead we collect
        a (write, notify) candidate pair for every service that has a notify
        characteristic, subscribe to all of them at connect time, and lock in
        whichever service actually delivers a status frame.
        """
        services = list(client.services)

        for s_idx, service in enumerate(services):
            _LOGGER.debug("XD fan service[%d] uuid=%s", s_idx, service.uuid)
            for char in service.characteristics:
                _LOGGER.debug(
                    "  char uuid=%s props=%s",
                    char.uuid,
                    ",".join(char.properties),
                )

        def _pick_from_service(service) -> tuple:
            # Prefer a response-capable ``write`` characteristic; only fall
            # back to write-without-response when no response-capable write
            # exists on this service.
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

        candidates: list[tuple] = []
        write_only = None
        for service in services:
            w, n = _pick_from_service(service)
            if w is not None and n is not None:
                candidates.append((w, n))
                _LOGGER.debug(
                    "XD fan candidate service uuid=%s write=%s notify=%s",
                    service.uuid,
                    w.uuid,
                    n.uuid,
                )
            elif w is not None and write_only is None:
                write_only = w

        self._candidates = candidates
        self._channel_locked = False
        self._notify_chars = [n for _w, n in candidates]

        if candidates:
            # Default to the first candidate until a notify frame locks the
            # real channel; keeps control working even before any echo.
            self._write_char, self._notify_char = candidates[0]
        else:
            # No service exposes both; fall back to any writable char so at
            # least control commands can be sent.
            self._write_char = write_only
            self._notify_char = None

        _LOGGER.debug(
            "XD fan candidates=%d, default write=%s notify=%s",
            len(candidates),
            getattr(self._write_char, "uuid", None),
            getattr(self._notify_char, "uuid", None),
        )
        if self._write_char is None:
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

    async def _write(self, packet: list[int], char=None) -> None:
        async with self._lock:
            if not self._client or not self._client.is_connected:
                _LOGGER.warning("Write skipped, XD fan not connected")
                return
            write_char = char if char is not None else self._write_char
            if write_char is None:
                _LOGGER.warning("Write skipped, no write characteristic")
                return
            data = bytes(packet)
            # The original app used write-with-response by default. Prefer it
            # when the characteristic supports it, falling back to
            # write-without-response only when it is the sole option.
            props = write_char.properties
            use_response = "write" in props
            if not use_response and "write-without-response" not in props:
                use_response = True
            _LOGGER.debug(
                "XD fan write (response=%s) to %s: %s",
                use_response,
                write_char.uuid,
                data.hex(" "),
            )
            try:
                await self._client.write_gatt_char(
                    write_char, data, response=use_response
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "XD fan write failed (response=%s): %s; retrying",
                    use_response,
                    err,
                )
                await self._client.write_gatt_char(
                    write_char, data, response=not use_response
                )
        # Pace consecutive writes like the original app.
        await asyncio.sleep(WRITE_DELAY)

    def _on_notify(
        self, _char: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle a notify packet and update state."""
        n = list(data)
        if len(n) < 15:
            _LOGGER.debug("Ignoring short notify packet: %s", bytes(data).hex())
            return
        # First valid frame locks the real command channel to the service
        # this notification came from, so subsequent control/query writes go
        # to the characteristic the device actually listens on.
        if not self._channel_locked:
            for w, nf in self._candidates:
                if nf.uuid == _char.uuid:
                    self._write_char = w
                    self._notify_char = nf
                    self._channel_locked = True
                    _LOGGER.debug(
                        "XD fan channel locked: write=%s notify=%s",
                        w.uuid,
                        nf.uuid,
                    )
                    break
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
        _LOGGER.debug("XD fan state updated: %s", s.as_dict())
        self._notify_listeners()

    def _on_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.debug("XD fan %s disconnected", self._address)
        self.state.available = False
        self._notify_listeners()

    def _notify_listeners(self) -> None:
        for cb in list(self._update_callbacks):
            cb()