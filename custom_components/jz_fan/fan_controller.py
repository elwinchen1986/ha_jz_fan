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


def _is_standard_service(uuid: str) -> bool:
    """Return True for the standard GATT services we should skip.

    0x1800 (Generic Access) and 0x1801 (Generic Attribute) never carry the
    fan's private control/notify characteristics. bleak may enumerate them at
    an index the mini-program assumed was a private service, so we skip them
    explicitly rather than relying on enumeration order.
    """
    u = str(uuid).lower()
    return u.startswith("00001800") or u.startswith("00001801") or u in (
        "1800",
        "1801",
    )


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
        # Primary write characteristic (first private-service writable char).
        self._write_char: BleakGATTCharacteristic | None = None
        # Every writable characteristic found across private services. The
        # mini-program wrote to *each* write characteristic of the target
        # service, so we mirror that "write to all" behaviour.
        self._write_chars: list[BleakGATTCharacteristic] = []
        # Every notify/indicate characteristic found across private services.
        # The mini-program subscribed to *all* of them (its global
        # onBLECharacteristicValueChange handler received data from any of
        # them), so we subscribe to all rather than guessing one.
        self._notify_chars: list[BleakGATTCharacteristic] = []
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
            self.state.available = True
            self._notify_listeners()

        # The mini-program's per-characteristic loop wrote the handshake
        # packets *before* it enabled notifications, then subscribed. Some
        # fan firmwares only switch into "report mode" after they receive the
        # handshake, so subscribing first (as we did previously) left the
        # CCCD enabled but the device never pushing. Mirror the app order:
        #   1. write handshake packets to every writable characteristic
        #   2. subscribe to every notify/indicate characteristic
        #   3. send the query packet so the device echoes its full state
        # Each write is paced by WRITE_DELAY (666ms) like the original app.

        # 1. Handshake first (device enters report mode).
        for pkt in INIT_PACKETS:
            await self._write(pkt)

        # 2. Subscribe to *every* notify/indicate characteristic. The
        # mini-program's global value-change handler received frames from any
        # of them, and on this device the echo can arrive on a notify
        # characteristic other than the one we would have guessed.
        async with self._lock:
            subscribed: list[str] = []
            for notify_char in self._notify_chars:
                try:
                    _LOGGER.info(
                        "Subscribing to notify on %s (props=%s)",
                        notify_char.uuid,
                        ",".join(notify_char.properties),
                    )
                    await client.start_notify(notify_char, self._on_notify)
                    subscribed.append(notify_char.uuid)
                    # Explicitly enable the CCCD descriptor. Some bleak
                    # backends do not reliably write 0x2902 during
                    # start_notify, which leaves the device silent.
                    await self._enable_cccd(client, notify_char)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error(
                        "XD fan start_notify on %s failed: %s",
                        notify_char.uuid,
                        err,
                    )
            if subscribed:
                _LOGGER.info(
                    "XD fan subscribed to notify chars: %s",
                    ", ".join(subscribed),
                )
            else:
                _LOGGER.error(
                    "XD fan has no notify characteristic subscribed; "
                    "state will not update"
                )

        # 3. Query the full status frame now that the device is in report
        # mode and we are listening. This is what populates the initial echo.
        await self._write(QUERY_PACKET)

        _LOGGER.info(
            "XD fan connected and initialized: write=%s notify=%s "
            "subscribed=%s",
            getattr(self._write_char, "uuid", None),
            [c.uuid for c in self._notify_chars],
            len(self._notify_chars) > 0,
        )

    async def async_disconnect(self) -> None:
        self.stop_polling()
        async with self._lock:
            if self._client and self._client.is_connected:
                try:
                    for notify_char in self._notify_chars:
                        try:
                            await self._client.stop_notify(notify_char)
                        except Exception:  # noqa: BLE001
                            pass
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
                        _LOGGER.info(
                            "XD fan poll: link not connected, reconnecting"
                        )
                        await self.async_connect()
                    else:
                        await self.async_query_state()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning(
                        "XD fan poll iteration failed: %s", err
                    )
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
        """Collect writable and notify characteristics on private services.

        The WeChat mini-program subscribed to *every* notify/indicate
        characteristic on the target service (its global value-change
        handler received frames from any of them) and wrote to *every*
        writable characteristic. It relied on service ordering (services[2])
        which is not portable: bleak enumerates services in a different order
        than WeChat, and standard GATT services (0x1800/0x1801) may appear at
        that index.

        So instead of guessing one service by index, we scan *all* private
        services (UUIDs that are not the standard 0x1800/0x1801) and gather
        every writable and every notify/indicate characteristic. We then
        subscribe to all notify characteristics and write to the primary
        writable one, mirroring the mini-program's "subscribe/write to all"
        behaviour without depending on enumeration order.
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

        write_chars: list[BleakGATTCharacteristic] = []
        write_no_resp_chars: list[BleakGATTCharacteristic] = []
        notify_chars: list[BleakGATTCharacteristic] = []

        for service in services:
            if _is_standard_service(service.uuid):
                # Skip Generic Access (0x1800) / Generic Attribute (0x1801);
                # the device's control/notify lives on its private services.
                continue
            for char in service.characteristics:
                props = char.properties
                if "write" in props:
                    write_chars.append(char)
                elif "write-without-response" in props:
                    write_no_resp_chars.append(char)
                if "notify" in props or "indicate" in props:
                    notify_chars.append(char)

        # If no private service exposed a writable characteristic (unexpected),
        # fall back to scanning *all* services so we still function.
        if not write_chars and not write_no_resp_chars and not notify_chars:
            for service in services:
                for char in service.characteristics:
                    props = char.properties
                    if "write" in props:
                        write_chars.append(char)
                    elif "write-without-response" in props:
                        write_no_resp_chars.append(char)
                    if "notify" in props or "indicate" in props:
                        notify_chars.append(char)

        # Prefer write-with-response characteristics (the mini-program's
        # default write type); keep write-without-response ones as extras.
        all_writes = write_chars + write_no_resp_chars
        self._write_chars = all_writes
        self._write_char = all_writes[0] if all_writes else None
        self._notify_chars = notify_chars

        _LOGGER.info(
            "XD fan selected: write=%s writes=%s notify=%s",
            getattr(self._write_char, "uuid", None),
            [c.uuid for c in all_writes],
            [c.uuid for c in notify_chars],
        )
        if not notify_chars:
            _LOGGER.warning(
                "XD fan found no notify/indicate characteristic; the device "
                "cannot echo its state - check the GATT layout logged above"
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

    async def _write(self, packet: list[int]) -> None:
        async with self._lock:
            if not self._client or not self._client.is_connected:
                _LOGGER.warning("Write skipped, XD fan not connected")
                return
            if self._write_char is None:
                _LOGGER.warning("Write skipped, no write characteristic")
                return
            data = bytes(packet)
            targets = self._write_chars or [self._write_char]
            # We do not know which private service is the device's real
            # command channel, and the status echo only starts once the
            # handshake reaches it. So write the packet to *every* writable
            # characteristic; the device ignores frames on the wrong channel.
            for idx, wchar in enumerate(targets):
                props = wchar.properties
                use_response = "write" in props
                if not use_response and \
                        "write-without-response" not in props:
                    use_response = True
                _LOGGER.info(
                    "XD fan write (response=%s, len=%d) to %s: %s",
                    use_response,
                    len(data),
                    wchar.uuid,
                    data.hex(" "),
                )
                try:
                    await self._client.write_gatt_char(
                        wchar, data, response=use_response
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning(
                        "XD fan write to %s failed (response=%s) -> %s; "
                        "retrying with response=%s",
                        wchar.uuid,
                        use_response,
                        err,
                        not use_response,
                    )
                    try:
                        await self._client.write_gatt_char(
                            wchar, data, response=not use_response
                        )
                    except Exception as err2:  # noqa: BLE001
                        _LOGGER.error(
                            "XD fan write to %s retry also failed: %s",
                            wchar.uuid,
                            err2,
                        )
                if idx < len(targets) - 1:
                    await asyncio.sleep(0.1)
        # Pace consecutive writes like the original app.
        await asyncio.sleep(WRITE_DELAY)

    async def _enable_cccd(
        self, client: BleakClient, notify_char: BleakGATTCharacteristic
    ) -> None:
        """Explicitly write the Client Characteristic Config descriptor.

        Some bleak backends do not reliably write the CCCD (0x2902) when
        start_notify is called, so the device never actually starts pushing
        notifications. We locate the 0x2902 descriptor on the characteristic
        and write [0x01, 0x00] (notify) or [0x02, 0x00] (indicate). Failures
        are non-fatal: many backends already handled it inside start_notify.
        """
        try:
            cccd = None
            for descriptor in notify_char.descriptors:
                if str(descriptor.uuid).lower().startswith("00002902"):
                    cccd = descriptor
                    break
            if cccd is None:
                return
            if "indicate" in notify_char.properties:
                value = bytes([0x02, 0x00])
            else:
                value = bytes([0x01, 0x00])
            await client.write_gatt_descriptor(cccd.handle, value)
            _LOGGER.info(
                "XD fan CCCD written on %s: %s",
                notify_char.uuid,
                value.hex(" "),
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "XD fan CCCD write on %s skipped: %s",
                notify_char.uuid,
                err,
            )

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